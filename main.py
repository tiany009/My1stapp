from kivy.app import App
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
