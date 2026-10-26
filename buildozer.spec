[app]
title = PS5 Memory Scanner
package.name = myfirstapp
package.domain = org.pocketapk
source.dir = .
source.include_exts = py,kv,png,jpg,atlas,json
source.exclude_dirs = .git,.github,bin,tests
version = 0.2
requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0,charset-normalizer==2.1.1
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.archs = arm64-v8a
android.api = 33
android.minapi = 24
android.ndk = 25b
android.ndk_api = 24
android.accept_sdk_license = True
android.debug_artifact = apk
p4a.branch = v2024.01.21

[buildozer]
log_level = 2
warn_on_root = 1
