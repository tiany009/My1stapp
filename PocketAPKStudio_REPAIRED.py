"""Pocket APK Studio - a Pydroid 3 Kivy project editor and Buildozer frontend."""
import os
import sys
import traceback
import tempfile
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from datetime import datetime

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
from kivy.metrics import dp

def project_root():
    candidates = [Path(os.environ.get('POCKET_APK_HOME', str(Path.home() / 'PocketAPKProjects'))),
                  Path(tempfile.gettempdir()) / 'PocketAPKProjects']
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    raise RuntimeError('No writable project directory found')

ROOT = None

SAMPLE = '''from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button

class DemoApp(App):
    def build(self):
        root = BoxLayout(orientation="vertical", padding=30, spacing=20)
        root.add_widget(Label(text="Hello from my Android app!"))
        btn = Button(text="Tap me")
        btn.bind(on_press=lambda *_: setattr(btn, "text", "It works!"))
        root.add_widget(btn)
        return root

DemoApp().run()
'''


def spec_text(title, package):
    return f'''[app]
title = {title}
package.name = {package}
package.domain = org.pocketapk
source.dir = .
source.include_exts = py,kv,png,jpg,atlas,json
version = 0.1
requirements = python3,kivy
orientation = portrait
fullscreen = 0
android.archs = arm64-v8a
android.minapi = 24

[buildozer]
log_level = 2
warn_on_root = 1
'''


class Studio(App):
    def build(self):
        global ROOT
        ROOT = project_root()
        self.title = 'Pocket APK Studio'
        self.project = None
        self.running = False
        self.root_ui = BoxLayout(orientation='vertical', spacing=dp(5), padding=dp(7))
        self.root_ui.add_widget(Label(text='POCKET APK STUDIO', size_hint_y=None, height=dp(40), bold=True, font_size='20sp'))
        self.status = Label(text='Create or open a project', size_hint_y=None, height=dp(28))
        self.root_ui.add_widget(self.status)
        self.project_name_input = TextInput(text='MyFirstApp', multiline=False, hint_text='Project name', size_hint_y=None, height=dp(44))
        self.root_ui.add_widget(self.project_name_input)
        bar = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(4))
        for caption, cb in [('New', self.create), ('Open', self.open_projects), ('Save', self.save), ('Settings', self.settings)]:
            b = Button(text=caption)
            b.bind(on_press=cb)
            bar.add_widget(b)
        self.root_ui.add_widget(bar)
        self.editor = TextInput(text=SAMPLE, multiline=True, font_size='14sp', tab_width=4)
        self.root_ui.add_widget(self.editor)
        bar2 = BoxLayout(size_hint_y=None, height=dp(50), spacing=dp(4))
        for caption, cb in [('Preview', self.preview), ('Check tools', self.check_tools), ('Build APK', self.build_apk), ('APKs', self.show_apks)]:
            b = Button(text=caption)
            b.bind(on_press=cb)
            bar2.add_widget(b)
        self.root_ui.add_widget(bar2)
        self.log = TextInput(text='Ready. Local APK builds require a compatible Linux Buildozer toolchain.\n', readonly=True, size_hint_y=None, height=dp(155), font_size='12sp')
        self.root_ui.add_widget(self.log)
        return self.root_ui

    def info(self, message):
        self.status.text = message[:75]
        self.log.text += message + '\n'
        self.log.cursor = (0, len(self.log.text.splitlines()))

    def popup(self, title, message):
        Popup(title=title, content=Label(text=message, text_size=(dp(300), None)), size_hint=(.92, .5)).open()

    def create(self, *_):
        name = self.project_name_input.text.strip()
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{1,39}', name):
            self.popup('Invalid name', 'Use 2–40 letters, digits, or underscores. Start with a letter.')
            return
        path = ROOT / name
        if path.exists():
            self.popup('Already exists', 'Choose Open to edit this project.')
            return
        path.mkdir(parents=True)
        (path / 'main.py').write_text(SAMPLE, encoding='utf-8')
        (path / 'buildozer.spec').write_text(spec_text(name, re.sub('[^a-z0-9]', '', name.lower())), encoding='utf-8')
        self.project = path
        self.editor.text = SAMPLE
        self.info('Created: ' + str(path))

    def open_projects(self, *_):
        names = sorted([p for p in ROOT.iterdir() if p.is_dir() and (p / 'main.py').exists()])
        if not names:
            self.popup('No projects', 'Create a project first.')
            return
        content = BoxLayout(orientation='vertical')
        scroll = ScrollView()
        items = BoxLayout(orientation='vertical', size_hint_y=None, spacing=dp(4))
        items.bind(minimum_height=items.setter('height'))
        pop = Popup(title='Open project', content=content, size_hint=(.9, .7))
        for p in names:
            btn = Button(text=p.name, size_hint_y=None, height=dp(48))
            def select(_, project=p):
                self.project = project
                self.project_name_input.text = project.name
                self.editor.text = (project / 'main.py').read_text(encoding='utf-8')
                self.info('Opened: ' + str(project))
                pop.dismiss()
            btn.bind(on_press=select)
            items.add_widget(btn)
        scroll.add_widget(items)
        content.add_widget(scroll)
        pop.open()

    def save(self, *_):
        if not self.project:
            self.popup('No project', 'Create or open a project first.')
            return False
        (self.project / 'main.py').write_text(self.editor.text, encoding='utf-8')
        self.info('Saved main.py')
        return True

    def settings(self, *_):
        if not self.project:
            self.popup('No project', 'Create or open a project first.')
            return
        path = self.project / 'buildozer.spec'
        layout = BoxLayout(orientation='vertical')
        edit = TextInput(text=path.read_text(encoding='utf-8'), multiline=True)
        layout.add_widget(edit)
        popup = Popup(title='Buildozer settings', content=layout, size_hint=(.94, .85))
        btn = Button(text='Save settings', size_hint_y=None, height=dp(50))
        def do_save(*_):
            path.write_text(edit.text, encoding='utf-8')
            self.info('Saved buildozer.spec')
            popup.dismiss()
        btn.bind(on_press=do_save)
        layout.add_widget(btn)
        popup.open()

    def preview(self, *_):
        if not self.save():
            return
        try:
            compile(self.editor.text, str(self.project / 'main.py'), 'exec')
        except SyntaxError as ex:
            self.info('Syntax error at line %s: %s' % (ex.lineno, ex.msg))
            return
        self.info('Python syntax OK. Run the project main.py in Pydroid to preview.')

    def tool_report(self):
        lines = ['Platform: ' + os.name, 'Buildozer: ' + str(shutil.which('buildozer')),
                 'Java: ' + str(shutil.which('java')), 'Git: ' + str(shutil.which('git')),
                 'C compiler: ' + str(shutil.which('clang') or shutil.which('gcc')),
                 'ANDROID_HOME: ' + str(os.getenv('ANDROID_HOME')),
                 'ANDROID_NDK_HOME: ' + str(os.getenv('ANDROID_NDK_HOME'))]
        if not shutil.which('buildozer'):
            lines.append('Build unavailable: Buildozer not found on this Python environment.')
        else:
            lines.append('Buildozer found. This does not guarantee ARM64 on-device compilation works.')
        return '\n'.join(lines)

    def check_tools(self, *_):
        self.info(self.tool_report())

    def build_apk(self, *_):
        if self.running:
            self.info('A build is already running.')
            return
        if not self.save():
            return
        if not shutil.which('buildozer'):
            self.info('Cannot build: Buildozer is missing. See SETUP.md for requirements.')
            return
        self.running = True
        self.info('Starting real Buildozer build. This can take a long time.')
        threading.Thread(target=self._build_worker, daemon=True).start()

    def _build_worker(self):
        logfile = self.project / 'pocket_build.log'
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        before = set((self.project / 'bin').glob('*.apk')) if (self.project / 'bin').exists() else set()
        code = -1
        try:
            with logfile.open('w', encoding='utf-8') as log:
                process = subprocess.Popen(['buildozer', '-v', 'android', 'debug'], cwd=str(self.project),
                                           env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, bufsize=1)
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    Clock.schedule_once(lambda dt, msg=line.strip(): self.info(msg) if msg else None)
                code = process.wait()
        except Exception as ex:
            Clock.schedule_once(lambda dt, msg=str(ex): self.info('Build launch error: ' + msg))
        files = list((self.project / 'bin').glob('*.apk')) if (self.project / 'bin').exists() else []
        valid = [p for p in files if p.is_file() and p.stat().st_size > 10000 and (p not in before or p.stat().st_mtime >= logfile.stat().st_mtime)]
        if code == 0 and valid:
            result = 'APK BUILD SUCCESS: ' + str(max(valid, key=lambda p: p.stat().st_mtime))
        else:
            result = 'BUILD FAILED or no new APK produced. Check ' + str(logfile)
        Clock.schedule_once(lambda dt: self._finish(result))

    def _finish(self, result):
        self.running = False
        self.info(result)

    def show_apks(self, *_):
        if not self.project:
            self.popup('No project', 'Create or open a project first.')
            return
        folder = self.project / 'bin'
        files = sorted(folder.glob('*.apk')) if folder.exists() else []
        if not files:
            self.popup('No APK yet', 'No APK files were found. An APK appears only after a successful build.')
            return
        self.popup('APK output', '\n'.join(str(p) for p in files[-3:]))

def start():
    try:
        Studio().run()
    except BaseException:
        error = traceback.format_exc()
        print(error, flush=True)
        paths = [Path('/storage/emulated/0/Download/pocket_apk_error.txt'),
                 Path.home() / 'pocket_apk_error.txt',
                 Path(tempfile.gettempdir()) / 'pocket_apk_error.txt']
        for path in paths:
            try:
                path.write_text(error, encoding='utf-8')
                print('Crash log saved:', path, flush=True)
                break
            except OSError:
                continue
        try:
            input('App failed. Press Enter to exit...')
        except (EOFError, OSError):
            pass

if __name__ == '__main__':
    start()
