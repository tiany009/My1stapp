"""PS5 game-memory scanner and explicit value editor."""
import socket
import struct
import threading
import math
from array import array

class DebugClient:
    def __init__(self, host):
        self.sock = socket.create_connection((host, 744), timeout=10)
        self.sock.settimeout(30)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    def close(self):
        self.sock.close()
    def recv(self, size):
        out = bytearray()
        while len(out) < size:
            data = self.sock.recv(size-len(out))
            if not data:
                raise ConnectionError('Debugger disconnected')
            out.extend(data)
        return bytes(out)
    def send(self, cmd, body=b''):
        self.sock.sendall(struct.pack('<III', 0xFFAABBCC, cmd, len(body))+body)
    def status(self):
        value, = struct.unpack('<I', self.recv(4))
        if value not in (0x80000000, 0x40000000):
            raise RuntimeError('Debugger status 0x%08X' % value)
    def branding(self):
        self.send(0xBD000501)
        size, = struct.unpack('<I', self.recv(4))
        if size > 4096:
            raise RuntimeError('Invalid branding response')
        return self.recv(size).split(b'\0')[0].decode('utf8', 'replace')
    def processes(self):
        self.send(0xBDAA0001)
        self.status()
        count, = struct.unpack('<I', self.recv(4))
        if count > 16384:
            raise RuntimeError('Invalid process count')
        return [(pid, name.split(b'\0')[0].decode('utf8','replace')) for name,pid in
                struct.iter_unpack('<32si', self.recv(count*36))]
    def maps(self, pid):
        self.send(0xBDAA0004, struct.pack('<I', pid))
        self.status()
        count, = struct.unpack('<I', self.recv(4))
        if count > 65536:
            raise RuntimeError('Invalid map count')
        return list(struct.iter_unpack('<32sQQQH', self.recv(count*58)))
    def read(self, pid, address, size):
        self.send(0xBDAA0002, struct.pack('<IQI', pid, address, size))
        self.status()
        return self.recv(size)

    def write(self, pid, address, data):
        self.send(0xBDAA0003, struct.pack('<IQI', pid, address, len(data)))
        self.status()
        self.sock.sendall(data)
        self.status()

TYPES = {'Float32':'f', 'UInt32':'I', 'Int32':'i', 'UInt16':'H', 'UInt8':'B', 'Float64':'d'}
def compare_snapshot(old, new, fmt, mode, candidates=None):
    size = struct.calcsize('<'+fmt)
    unpack = struct.Struct('<'+fmt).unpack_from
    result = array('I')
    offsets = range(0, min(len(old),len(new))-size+1, size) if candidates is None else candidates
    for offset in offsets:
        a, b = unpack(old, offset)[0], unpack(new, offset)[0]
        if isinstance(a,float) and (not math.isfinite(a) or not math.isfinite(b)):
            continue
        match = ((mode=='Decreased' and b<a) or (mode=='Increased' and b>a) or
                 (mode=='Changed' and b!=a) or (mode=='Unchanged' and b==a))
        if match:
            result.append(offset)
    return result

import os
import json
import sqlite3
import zlib
import hashlib
import shutil

BLOCK = 1024 * 1024
MODES = {'Took damage': 'Decreased', 'Healed': 'Increased',
         'Health stayed the same': 'Unchanged', 'Value changed': 'Changed'}

class Cancelled(Exception):
    pass

class ScanStore:
    """Disk-backed snapshots; a failed filter rolls back as one transaction."""
    def __init__(self, path):
        self.path = path
    def open(self):
        db = sqlite3.connect(self.path)
        db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS blocks (address INTEGER PRIMARY KEY, data BLOB, masks TEXT)')
        return db
    @staticmethod
    def meta(db):
        return {k: json.loads(v) for k,v in db.execute('SELECT key,value FROM meta')}
    @staticmethod
    def put(db, key, value):
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key,json.dumps(value)))
    def info(self):
        with self.open() as db:
            return self.meta(db)
    def baseline(self, reader, plan, identity, kinds, progress, cancel):
        # Build separately so an interrupted new search cannot destroy the old one.
        stage = self.path + '.building'
        db = ScanStore(stage).open()
        try:
            m = self.meta(db)
            signature = [identity,kinds,[list(item) for item in plan]]
            if m.get('signature') != signature:
                db.execute('DELETE FROM blocks'); db.execute('DELETE FROM meta')
                self.put(db,'signature',signature); db.commit()
            total=sum(n for a,n in plan)
            done=sum(len(zlib.decompress(row[0])) for row in db.execute('SELECT data FROM blocks'))
            for address,length in plan:
                if cancel.is_set(): raise Cancelled('Baseline paused; tap Start / resume to continue.')
                if db.execute('SELECT 1 FROM blocks WHERE address=?',(address,)).fetchone(): continue
                if shutil.disk_usage(os.path.dirname(self.path)).free < length*3+16*1024*1024:
                    raise ValueError('Phone storage low. Baseline paused; free space or use a smaller range.')
                progress(f'Baseline: {done//1048576} / {total//1048576} MiB. Keep gameplay paused.')
                data=reader(address,length)
                if len(data)!=length: raise ValueError('Incomplete memory read')
                db.execute('INSERT INTO blocks VALUES (?,?,?)',(address,zlib.compress(data,1),'null'))
                db.commit(); done+=length
            self.put(db,'identity',identity); self.put(db,'kinds',kinds)
            self.put(db,'round',0); self.put(db,'bytes',total)
            self.put(db,'summary','Baseline saved. Take damage, pause, then use Took damage.')
            db.commit()
        finally:
            db.close()
        os.replace(stage,self.path)
    def filter(self, reader, identity, mode, progress, cancel):
        db=self.open()
        try:
            m=self.meta(db)
            if m.get('identity')!=identity: raise ValueError('Game identity or mappings changed. Start a new search.')
            addresses=[r[0] for r in db.execute('SELECT address FROM blocks ORDER BY address')]
            if not addresses: raise ValueError('Start a baseline first')
            count=0
            db.execute('BEGIN')
            for index,address in enumerate(addresses):
                if cancel.is_set(): raise Cancelled('Filter cancelled. Previous completed scan retained.')
                raw,masks=db.execute('SELECT data,masks FROM blocks WHERE address=?',(address,)).fetchone()
                old=zlib.decompress(raw); masks=json.loads(masks)
                if masks is not None and not any(masks.values()): continue
                progress(f'Filtering block {index+1} / {len(addresses)}. Keep gameplay paused.')
                new=reader(address,len(old))
                if len(new)!=len(old): raise ValueError('Incomplete memory read')
                next_masks={}
                for kind in m['kinds']:
                    offsets=None if masks is None else masks[kind]
                    result=compare_snapshot(old,new,TYPES[kind],mode,offsets)
                    next_masks[kind]=list(result); count+=len(result)
                db.execute('UPDATE blocks SET data=?,masks=? WHERE address=?',
                           (zlib.compress(new,1),json.dumps(next_masks),address))
            self.put(db,'round',m['round']+1)
            self.put(db,'count',count)
            self.put(db,'summary',f'{count:,} candidates. Round {m["round"]+1}; compared with previous completed scan.')
            db.commit()
            return count
        except BaseException:
            db.rollback(); raise
        finally: db.close()
    def results(self, limit=200):
        out=[]
        with self.open() as db:
            for address,raw,masks in db.execute('SELECT address,data,masks FROM blocks ORDER BY address'):
                masks=json.loads(masks)
                if masks is None: continue
                data=zlib.decompress(raw)
                for kind,offsets in masks.items():
                    for offset in offsets:
                        value=struct.unpack_from('<'+TYPES[kind],data,offset)[0]
                        out.append((address+offset,kind,value))
                        if len(out)>=limit: return out
        return out

def make_plan(maps, limit):
    # Anonymous writable memory first; then game data, then library data.
    regions=[]
    for raw,start,end,offset,prot in maps:
        if prot&3!=3 or end<=start: continue
        name=raw.split(b'\0')[0].decode('utf8','replace')
        priority=0 if not name or 'anon' in name.lower() else (2 if '.prx' in name else 1)
        regions.append((priority,start,end))
    plan=[]; used=0
    for _,start,end in sorted(regions):
        for base in range(start,end,BLOCK):
            size=min(BLOCK,end-base)
            if limit: size=min(size,limit-used)
            if size<=0: return plan
            plan.append((base,size)); used+=size
    return plan

def memory_identity(host,pid,name,maps):
    layout=[(r.hex(),a,b,o,p) for r,a,b,o,p in maps]
    digest=hashlib.sha256(json.dumps(layout).encode()).hexdigest()
    return [host,pid,name,digest]

def checked_write(client,pid,address,kind,expected,replacement,maps):
    """Single explicit write; never replay an uncertain write automatically."""
    if not any(a<=address and address+len(replacement)<=b and p&3==3
               for raw,a,b,o,p in maps): raise ValueError('Address is no longer writable')
    before=client.read(pid,address,len(expected))
    if before!=expected: raise ValueError('Value changed since preview. Read it again before editing.')
    client.write(pid,address,replacement)
    actual=client.read(pid,address,len(replacement))
    if actual!=replacement: raise ValueError('Read-back differs. Game may overwrite this value; write not verified.')
    return before

def run_app():
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.metrics import dp,sp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.label import Label
    from kivy.uix.textinput import TextInput
    from kivy.uix.spinner import Spinner
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.popup import Popup
    class ScannerApp(App):
        def build(self):
            self.busy=False; self.cancel=threading.Event(); self.processes={}; self.rows={}; self.undo=None
            self.store=ScanStore(os.path.join(self.user_data_dir,'smart-scan.sqlite'))
            root=BoxLayout(orientation='vertical',padding=dp(10),spacing=dp(6))
            title=Label(text='PS5 Smart Scanner + Editor 0.3',size_hint_y=None,height=dp(32))
            root.add_widget(title)
            scroller=ScrollView(size_hint_y=.53)
            controls=BoxLayout(orientation='vertical',size_hint_y=None,spacing=dp(6))
            controls.bind(minimum_height=controls.setter('height'));scroller.add_widget(controls);root.add_widget(scroller)
            self.locked=[]
            def add(widget,lock=True):
                controls.add_widget(widget)
                if lock:self.locked.append(widget)
                return widget
            def button(text,action):
                b=Button(text=text,size_hint_y=None,height=dp(46),font_size=sp(16))
                b.bind(on_release=lambda *_:self.work(action));return add(b)
            self.host=add(TextInput(text='192.168.12.167',multiline=False,size_hint_y=None,height=dp(46)))
            button('Connect / refresh games',self.connect)
            self.proc=add(Spinner(text='Select game process',size_hint_y=None,height=dp(46)))
            self.scope=add(Spinner(text='First 256 MiB (anonymous first)',values=['First 256 MiB (anonymous first)','First 1 GiB (anonymous first)','All writable memory'],size_hint_y=None,height=dp(46)))
            self.kind=add(Spinner(text='Auto: Float32 + Int32 + UInt32',values=['Auto: Float32 + Int32 + UInt32']+list(TYPES),size_hint_y=None,height=dp(46)))
            button('Start / resume broad baseline',self.start)
            self.mode=add(Spinner(text='Took damage',values=list(MODES),size_hint_y=None,height=dp(46)))
            button('Filter saved search',self.filter)
            cancel=Button(text='Cancel / pause scan',size_hint_y=None,height=dp(42))
            cancel.bind(on_release=lambda *_:self.cancel.set());add(cancel,False)
            self.result=add(Spinner(text='Select candidate to edit',size_hint_y=None,height=dp(46)))
            button('Read candidate / open editor',self.preview)
            button('Undo last verified edit',self.undo_edit)
            self.output=TextInput(readonly=True,font_size=sp(17),size_hint_y=.47)
            root.add_widget(self.output)
            self.show('Connect, choose the game, then capture a baseline with gameplay paused.\nAuto searches three value types across many sections.\n256 MiB is partial coverage; expand the range if needed. Results are unverified candidates.\nSaved search: '+self.store.info().get('summary','none'))
            return root
        def show(self,text):
            def update(dt):
                self.output.text=text; self.output.cursor=(0,0);self.output.scroll_y=0;self.output.scroll_x=0
            Clock.schedule_once(update)
        def work(self,action):
            if self.busy:return
            self.busy=True;self.cancel.clear()
            self.inputs=(self.host.text.strip(),self.proc.text,self.scope.text,self.kind.text,self.mode.text,self.result.text)
            for widget in self.locked:widget.disabled=True
            self.show('Working… Keep gameplay paused while memory is captured.')
            def task():
                try:action()
                except Exception as exc:self.show(str(exc)+'\nPrevious completed search is saved. Retry the filter after a read failure. A write error may mean the value changed—read it again.')
                finally:Clock.schedule_once(self.unlock)
            threading.Thread(target=task,daemon=True).start()
        def unlock(self,dt):
            self.busy=False
            for widget in self.locked:widget.disabled=False
        def session(self):
            # A new socket for each action avoids reusing an idle-aborted connection.
            client=DebugClient(self.inputs[0])
            try:
                client.branding()
                return client
            except BaseException:client.close();raise
        def connect(self):
            client=self.session()
            try:entries=client.processes()
            finally:client.close()
            self.processes={f'{p}: {n}':(p,n) for p,n in entries if n=='eboot.bin'}
            if not self.processes:raise ValueError('No eboot.bin found. Open the game and refresh.')
            def update(dt):
                self.proc.values=list(self.processes)
                self.proc.text=next(iter(self.processes)) if len(self.processes)==1 else 'Select game process'
            Clock.schedule_once(update)
            self.show(f'{len(self.processes)} game/app processes found. Choose the running game. Existing saved search retained.')
        def selected(self):
            selected=self.processes.get(self.inputs[1])
            if selected is None:raise ValueError('Connect and select the game first')
            return selected
        def validate(self,client):
            pid,name=self.selected()
            if (pid,name) not in client.processes():raise ValueError('Game process changed. Refresh and start a new search.')
            maps=client.maps(pid)
            return pid,maps,memory_identity(self.inputs[0],pid,name,maps)
        def reader(self,client,pid,maps,identity):
            holder=[client]
            def read(address,length):
                for attempt in range(2):
                    try:
                        data=bytearray()
                        for delta in range(0,length,65536):
                            if self.cancel.is_set():raise Cancelled('Scan paused. Previous completed filter retained.')
                            data.extend(holder[0].read(pid,address+delta,min(65536,length-delta)))
                        return bytes(data)
                    except (OSError,ConnectionError):
                        holder[0].close()
                        if attempt:raise
                        self.show('Read interrupted. Opening a fresh connection; saved results retained.')
                        holder[0]=self.session()
                        _,_,current=self.validate(holder[0])
                        if current!=identity:raise ValueError('Game mappings changed; start a new baseline.')
            return read,holder
        def start(self):
            client=self.session();holder=[client]
            try:
                pid,maps,identity=self.validate(client)
                kinds=['Float32','Int32','UInt32'] if self.inputs[3].startswith('Auto:') else [self.inputs[3]]
                limit=256*1048576 if self.inputs[2].startswith('First 256') else (1024*1048576 if self.inputs[2].startswith('First 1 ') else 0)
                plan=make_plan(maps,limit)
                if not plan:raise ValueError('No writable regions found')
                read,holder=self.reader(client,pid,maps,identity)
                self.store.baseline(read,plan,identity,kinds,self.show,self.cancel)
                self.render()
            finally:holder[0].close()
        def filter(self):
            client=self.session();holder=[client]
            try:
                pid,maps,identity=self.validate(client)
                read,holder=self.reader(client,pid,maps,identity)
                self.store.filter(read,identity,MODES[self.inputs[4]],self.show,self.cancel)
                self.render()
            finally:holder[0].close()
        def render(self):
            info=self.store.info();results=self.store.results()
            rows={f'0x{a:X} | {k} | {v:.8g}':(a,k) for a,k,v in results}
            self.rows=rows
            def update(dt):
                self.result.values=list(rows);self.result.text='Select candidate to edit'
            Clock.schedule_once(update)
            text=info.get('summary','No baseline')+f'\nCoverage: {info.get("bytes",0)//1048576} MiB. Auto types: {", ".join(info.get("kinds",[]))}.\n'
            text+='\n'.join(rows)
            if len(results)==200:text+='\nShowing first 200; filter further to narrow the list.'
            text+='\nCandidates are not confirmed health. Test movement without damage, then healing. Keep the same game session.'
            self.show(text)
        def preview(self):
            row=self.rows.get(self.inputs[5])
            if row is None:raise ValueError('Run filters and select a candidate first')
            address,kind=row;client=self.session()
            try:
                pid,maps,identity=self.validate(client)
                if self.store.info().get('identity')!=identity:raise ValueError('Saved scan belongs to a different session')
                raw=client.read(pid,address,struct.calcsize('<'+TYPES[kind]))
                value=struct.unpack('<'+TYPES[kind],raw)[0]
            finally:client.close()
            Clock.schedule_once(lambda dt:self.editor(address,kind,value,raw,identity))
        def editor(self,address,kind,value,raw,identity):
            box=BoxLayout(orientation='vertical',spacing=dp(10),padding=dp(12))
            box.add_widget(Label(text=f'0x{address:X}  {kind}\nCurrent: {value}\nUnverified candidate. A wrong value can crash the game.'))
            field=TextInput(text=str(value),multiline=False,size_hint_y=None,height=dp(50));box.add_widget(field)
            apply=Button(text='Apply this value once',size_hint_y=None,height=dp(48));box.add_widget(apply)
            close=Button(text='Cancel',size_hint_y=None,height=dp(44));box.add_widget(close)
            popup=Popup(title='Edit candidate — explicit write',content=box,size_hint=(.96,.68))
            def confirm(*_):
                try:
                    fmt=TYPES[kind];number=float(field.text) if fmt in ('f','d') else int(field.text,0) if field.text.lower().startswith(('0x','-0x')) else int(field.text)
                    if isinstance(number,float) and not math.isfinite(number):raise ValueError('Enter a finite number')
                    replacement=struct.pack('<'+fmt,number)
                except (ValueError,OverflowError,struct.error) as exc:
                    self.show('Invalid value: '+str(exc));return
                popup.dismiss()
                self.work(lambda:self.apply_edit(address,kind,raw,replacement,identity))
            apply.bind(on_release=confirm);close.bind(on_release=lambda *_:popup.dismiss());popup.open()
        def apply_edit(self,address,kind,expected,replacement,identity):
            client=self.session()
            try:
                pid,maps,current=self.validate(client)
                if current!=identity:raise ValueError('Game mappings changed; edit cancelled')
                checked_write(client,pid,address,kind,expected,replacement,maps)
                self.undo=(address,kind,replacement,expected,identity)
            finally:client.close()
            self.show(f'Write verified at 0x{address:X}. Old and new bytes saved for Undo.\nCheck the game effect; this does not prove the address is health. Undo works only if the value still matches this edit.')
        def undo_edit(self):
            if self.undo is None:raise ValueError('No verified edit to undo in this app session')
            args=self.undo
            self.apply_edit(*args)
            self.undo=None
    ScannerApp().run()

if __name__=='__main__':run_app()
