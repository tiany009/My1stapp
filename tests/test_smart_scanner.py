import importlib.util
import os
import struct
import tempfile
import threading
import unittest
spec=importlib.util.spec_from_file_location('scanner',os.path.join(os.path.dirname(__file__),'..','main.py'))
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

class Tests(unittest.TestCase):
    def test_broad_filter_transaction_and_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            store=s.ScanStore(os.path.join(folder,'scan.db'))
            cancel=threading.Event(); identity=['host',512,'eboot.bin','layout']
            memory={4096:struct.pack('<ff',100,55),8192:struct.pack('<ff',20,7)}
            reads=[]
            def read(a,n): reads.append(a);return memory[a]
            cancel.set()
            with self.assertRaises(s.Cancelled):store.baseline(read,[(4096,8),(8192,8)],identity,['Float32'],lambda _:None,cancel)
            cancel.clear()
            def failing(a,n):
                if a==8192:raise OSError('disconnect')
                return read(a,n)
            with self.assertRaises(OSError):store.baseline(failing,[(4096,8),(8192,8)],identity,['Float32'],lambda _:None,cancel)
            reads.clear()
            store.baseline(read,[(4096,8),(8192,8)],identity,['Float32'],lambda _:None,cancel)
            self.assertEqual(reads,[8192])
            memory[4096]=struct.pack('<ff',80,54);memory[8192]=struct.pack('<ff',19,7)
            with self.assertRaises(OSError):store.filter(failing,identity,'Decreased',lambda _:None,cancel)
            self.assertEqual(store.info()['round'],0)
            self.assertEqual(store.filter(read,identity,'Decreased',lambda _:None,cancel),3)
            self.assertEqual(store.filter(read,identity,'Unchanged',lambda _:None,cancel),3)
            memory[4096]=struct.pack('<ff',70,54)
            self.assertEqual(store.filter(read,identity,'Decreased',lambda _:None,cancel),1)
            self.assertEqual(store.results(),[(4096,'Float32',70.0)])
            with self.assertRaises(ValueError):store.filter(read,['other'],'Unchanged',lambda _:None,cancel)
    def test_plan_covers_multiple_regions_and_caps(self):
        maps=[(b'lib.prx',8192,12288,0,3),(b'anon',4096,8192,0,3),(b'code',0,4096,0,5)]
        self.assertEqual(s.make_plan(maps,6144),[(4096,4096),(8192,2048)])
    def test_write_requires_fresh_value_and_verifies(self):
        class Fake:
            value=struct.pack('<I',100)
            writes=0
            def read(self,*_):return self.value
            def write(self,pid,address,data):self.writes+=1;self.value=data
        c=Fake();maps=[(b'anon',4096,8192,0,3)]
        before=c.value;after=struct.pack('<I',200)
        s.checked_write(c,512,4096,'UInt32',before,after,maps)
        self.assertEqual(c.value,after)
        with self.assertRaises(ValueError):s.checked_write(c,512,4096,'UInt32',before,after,maps)
        self.assertEqual(c.writes,1)
        with self.assertRaises(ValueError):s.checked_write(c,512,0,'UInt32',after,before,maps)
    def test_write_protocol_handshake(self):
        class Socket:
            def __init__(self):self.sent=[]
            def sendall(self,data):self.sent.append(data)
        client=object.__new__(s.DebugClient);client.sock=Socket();acks=[]
        client.status=lambda:acks.append(len(client.sock.sent))
        client.write(512,4096,b'abcd')
        self.assertEqual(acks,[1,2])
        self.assertEqual(struct.unpack('<III',client.sock.sent[0][:12]),(0xFFAABBCC,0xBDAA0003,16))
        self.assertEqual(client.sock.sent[1],b'abcd')

if __name__=='__main__':unittest.main()
