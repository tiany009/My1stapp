"""Read-only PS5Debug-NG memory scanner. No game memory writes."""
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
        if value != 0x80000000:
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

def run_app():
    from kivy.app import App
    from kivy.clock import Clock
    from kivy.metrics import dp
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.label import Label
    from kivy.uix.button import Button
    from kivy.uix.textinput import TextInput
    from kivy.uix.spinner import Spinner
    class ScannerApp(App):
        def build(self):
            self.client=None
            self.busy=False
            self.snapshot=None
            self.candidates=None
            self.sections={}
            self.process_ids={}
            root=BoxLayout(orientation='vertical',padding=dp(12),spacing=dp(7))
            def label(text,height=30):
                root.add_widget(Label(text=text,size_hint_y=None,height=dp(height)))
            label('PS5 Memory Scanner — read only')
            self.host=TextInput(text='192.168.12.167',multiline=False,size_hint_y=None,height=dp(44))
            root.add_widget(self.host)
            self.controls=[]
            def button(text, action):
                b=Button(text=text,size_hint_y=None,height=dp(44))
                b.bind(on_release=lambda *_: self.work(action))
                root.add_widget(b)
                self.controls.append(b)
            button('Connect / refresh processes',self.connect)
            self.proc=Spinner(text='Select game process',size_hint_y=None,height=dp(44))
            root.add_widget(self.proc)
            self.proc.bind(text=lambda *_: self.reset())
            button('Load memory sections',self.load_maps)
            self.region=Spinner(text='Select memory section',size_hint_y=None,height=dp(44))
            root.add_widget(self.region)
            self.region.bind(text=lambda *_: self.reset())
            self.kind=Spinner(text='Float32',values=list(TYPES),size_hint_y=None,height=dp(42))
            root.add_widget(self.kind)
            self.kind.bind(text=lambda *_: self.reset())
            button('Start unknown-value scan',self.start_scan)
            self.mode=Spinner(text='Decreased',values=['Decreased','Increased','Changed','Unchanged'],size_hint_y=None,height=dp(42))
            root.add_widget(self.mode)
            button('Filter after bar changes',self.filter_scan)
            self.output=TextInput(text='Connect on the same home network. Select the game process, then a memory section. Each section covers up to 16 MiB. Start with a full bar, then let it drop and filter Decreased. No edits or freezing in this version.',readonly=True)
            root.add_widget(self.output)
            return root
        def reset(self):
            self.snapshot=None
            self.candidates=None
        def show(self,text):
            Clock.schedule_once(lambda dt: setattr(self.output,'text',text))
        def work(self, action):
            if self.busy:
                return
            self.busy=True
            # Capture all UI inputs on the UI thread.
            self.inputs=(self.host.text.strip(), self.proc.text,self.region.text,self.kind.text,self.mode.text)
            for control in self.controls+[self.proc,self.region,self.kind,self.mode,self.host]:
                control.disabled=True
            def task():
                try:
                    action()
                except Exception as exc:
                    self.show(str(exc)+'\nLast completed scan retained. Keep the game running; retry the filter. If the game restarted, reconnect and start a new scan.')
                    if self.client:
                        try: self.client.close()
                        except Exception: pass
                    self.client=None
                finally:
                    Clock.schedule_once(self.unlock)
            threading.Thread(target=task,daemon=True).start()
        def unlock(self,dt):
            self.busy=False
            for control in self.controls+[self.proc,self.region,self.kind,self.mode,self.host]:
                control.disabled=False
        def connect(self):
            if self.client: self.client.close()
            self.client=DebugClient(self.inputs[0])
            brand=self.client.branding()
            entries=self.client.processes()
            self.process_ids={f'{pid}: {name}':pid for pid,name in entries}
            self.sections={}
            self.reset()
            def update(dt):
                self.proc.values=list(self.process_ids)
                self.proc.text='Select game process'
                self.region.values=[]
                self.region.text='Select memory section'
            Clock.schedule_once(update)
            self.show('Connected: '+brand+'\nSelect the game process (often eboot.bin). Do not select a system process.')
        def load_maps(self):
            if not self.client: raise ValueError('Connect first')
            pid=self.process_ids.get(self.inputs[1])
            if pid is None: raise ValueError('Select the game process first')
            sections={}
            for raw,start,end,offset,prot in self.client.maps(pid):
                if prot&3 != 3 or end<=start: continue
                name=raw.split(b'\0')[0].decode('utf8','replace')
                for base in range(start,end,16*1024*1024):
                    length=min(end-base,16*1024*1024)
                    key=f'{base:X} ({length//1024} KiB) {name}'
                    sections[key]=(pid,base,length)
                    if len(sections)>20000: raise ValueError('Too many mapped sections')
            self.sections=sections
            self.reset()
            def update(dt):
                self.region.values=list(sections)
                self.region.text=next(iter(sections),'No readable/writable sections')
            Clock.schedule_once(update)
            self.show(f'{len(sections)} sections found. Select a section; start a new scan for each section or data type. A zero result means try another section/type.')
        def capture(self):
            # Retry a complete capture, never combine partial reads across sockets.
            section=self.sections.get(self.inputs[2])
            if section is None: raise ValueError('Load and select a memory section first')
            if self.snapshot is not None and self.scan_host != self.inputs[0]:
                raise ValueError('Host changed; reconnect and start a new scan')
            for attempt in range(2):
                try:
                    if self.client is None:
                        self.client=DebugClient(self.inputs[0])
                        self.client.branding()
                        pid,base,length=section
                        entries=self.client.processes()
                        expected=self.inputs[1].split(': ',1)[-1]
                        if (pid,expected) not in entries:
                            raise ValueError('Game process changed; reconnect and start a new scan')
                        maps=self.client.maps(pid)
                        if not any(start<=base and end>=base+length and prot&3==3
                                   for raw,start,end,offset,prot in maps):
                            raise ValueError('Memory mapping changed; reconnect and start a new scan')
                    return self.capture_once()
                except (OSError, ConnectionError):
                    if self.client:
                        try: self.client.close()
                        except Exception: pass
                    self.client=None
                    if attempt: raise
                    self.show('Connection interrupted. Reconnecting and retrying; previous scan retained.')
        def capture_once(self):
            if not self.client: raise ValueError('Connect first')
            section=self.sections.get(self.inputs[2])
            if section is None: raise ValueError('Load and select a memory section first')
            pid,base,length=section
            if pid != self.process_ids.get(self.inputs[1]): raise ValueError('Reload sections for this process')
            data=bytearray()
            for delta in range(0,length,65536):
                self.show(f'Reading memory: {delta*100//length}%')
                data.extend(self.client.read(pid,base+delta,min(65536,length-delta)))
            return bytes(data),section
        def start_scan(self):
            self.snapshot,section=self.capture()
            self.scan_host=self.inputs[0]
            self.scan_key=(section,self.inputs[3])
            self.candidates=None
            count=len(self.snapshot)//struct.calcsize('<'+TYPES[self.inputs[3]])
            self.show(f'Baseline captured: {count:,} possible values.\nLet the bar drop, pause gameplay, then filter Decreased. Full does not necessarily mean 100 in memory.')
        def filter_scan(self):
            if self.snapshot is None: raise ValueError('Start an unknown-value scan first')
            new,section=self.capture()
            if self.scan_key!=(section,self.inputs[3]): raise ValueError('Selection changed; start a new scan')
            fmt=TYPES[self.inputs[3]]
            self.show('Comparing values…')
            self.candidates=compare_snapshot(self.snapshot,new,fmt,self.inputs[4],self.candidates)
            self.snapshot=new
            lines=[f'{len(self.candidates):,} candidates remain. Comparisons use the previous scan.']
            for offset in self.candidates[:40]:
                value=struct.unpack_from('<'+fmt,new,offset)[0]
                lines.append(f'0x{section[1]+offset:X} = {value}')
            lines.append('Repeat after changing the bar. Results are candidates, not verified cheat codes. Addresses may change after restarting the game.')
            self.show('\n'.join(lines))
        def on_stop(self):
            if self.client:
                try: self.client.close()
                except Exception: pass
    ScannerApp().run()

if __name__=='__main__':
    run_app()
