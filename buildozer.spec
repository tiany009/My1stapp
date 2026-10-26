[app]
title = MyFirstApp
package.name = myfirstapp
package.domain = org.pocketapk
source.dir = .
source.include_exts = py,kv,png,jpg,atlas,json
source.exclude_dirs = .git,.github,bin,tests
version = 0.1
# Keep host and target Python aligned with the pinned p4a release.
# 2.1.1 has a universal pure-Python wheel, avoiding Android wheel tag rejection.
requirements = python3==3.11.5,hostpython3==3.11.5,kivy==2.3.0,charset-normalizer==2.1.1
orientation = portrait
fullscreen = 0
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

