"""Build a small macOS app launcher for this checkout and its virtualenv.

Python runs inside the bundle's process, so Dock, Cmd-Tab and permissions see
TeamsTranscriber rather than an unregistered command-line Python process.
Use the PyInstaller spec for a distributable app with bundled dependencies.
"""
import plistlib
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


def main():
    if sys.platform != 'darwin':
        raise SystemExit('This launcher requires macOS')
    root = Path(__file__).resolve().parent
    python = root / '.venv/bin/python'
    library = Path(sysconfig.get_config_var('LIBDIR')) / sysconfig.get_config_var('LDLIBRARY')
    if not python.is_file() or not library.is_file():
        raise SystemExit('Run this builder using .venv/bin/python')
    bundle = root / 'dist/TeamsTranscriber.app'
    macos, resources = bundle / 'Contents/MacOS', bundle / 'Contents/Resources'
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    source = root / 'build/macos-launcher.m'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(r'''
#import <Foundation/Foundation.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    @autoreleasepool {
        NSDictionary *info = NSBundle.mainBundle.infoDictionary;
        const char *python = [info[@"TranscriberPython"] fileSystemRepresentation];
        const char *script = [info[@"TranscriberScript"] fileSystemRepresentation];
        const char *library = [info[@"TranscriberLibrary"] fileSystemRepresentation];
        if (!python || !script || !library) return 1;
        void *handle = dlopen(library, RTLD_NOW | RTLD_GLOBAL);
        if (!handle) { fprintf(stderr, "%s\n", dlerror()); return 1; }
        int (*run)(int, char **) = dlsym(handle, "Py_BytesMain");
        if (!run) { fprintf(stderr, "%s\n", dlerror()); return 1; }
        char **args = calloc((size_t)argc + 2, sizeof(char *));
        if (!args) return 1;
        args[0] = (char *)python;
        args[1] = (char *)script;
        for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
        return run(argc + 1, args);
    }
}
''', encoding='utf-8')
    subprocess.run(['xcrun', 'clang', '-framework', 'Foundation', str(source),
                    '-o', str(macos / 'TeamsTranscriber')], check=True)
    with (bundle / 'Contents/Info.plist').open('wb') as stream:
        plistlib.dump({
            'CFBundleExecutable': 'TeamsTranscriber',
            'CFBundleIdentifier': 'io.github.lolkofka.teamstranscriber',
            'CFBundleName': 'TeamsTranscriber',
            'CFBundleDisplayName': 'Транскрибер',
            'CFBundlePackageType': 'APPL',
            'CFBundleVersion': '1',
            'CFBundleShortVersionString': '1.0',
            'CFBundleIconFile': 'icon.icns',
            'NSHighResolutionCapable': True,
            'NSMicrophoneUsageDescription': 'Микрофон для звонков Teams и записи голоса',
            'NSCameraUsageDescription': 'Камера для звонков Teams и записи видео',
            'TranscriberPython': str(python),
            'TranscriberScript': str(root / 'app.py'),
            'TranscriberLibrary': str(library),
        }, stream)
    shutil.copy2(root / 'ui/icon.icns', resources / 'icon.icns')
    subprocess.run(['codesign', '--force', '--sign', '-', str(bundle)], check=True)
    print(bundle)


if __name__ == '__main__':
    main()
