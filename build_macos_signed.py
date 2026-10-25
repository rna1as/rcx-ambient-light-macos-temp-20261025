"""Build isolated customer .app + DMG on a Mac. Never builds/flashes firmware.

Requires a Qt 5.15 SDK with QtSerialPort for the requested architecture, Xcode
command-line tools, Go >=1.23 and Python >=3.10. No Windows release helper runs.
"""
from pathlib import Path
import argparse
import datetime
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

def run(args, cwd, env):
    print('+', ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)

def build(args):
    if sys.platform != 'darwin':
        raise SystemExit('Native .app/DMG creation requires macOS; use verify_preparation.py on Windows.')
    qt = Path(args.qt).resolve()
    qmake = qt/'bin/qmake'
    # Homebrew Qt packages may omit macdeployqt or QtSerialPort; a CI job can
    # build the matching Qt tools/module and pass its isolated macdeployqt here.
    deploy = Path(os.environ.get('MACDEPLOYQT', str(qt/'bin/macdeployqt')))
    if not qmake.is_file() or not deploy.is_file():
        raise SystemExit('Missing qmake or macdeployqt in --qt SDK.')
    version = subprocess.check_output([str(qmake), '-query', 'QT_VERSION'], text=True).strip()
    if not version.startswith('5.15.'):
        raise SystemExit('This compatibility port requires Qt 5.15.x; Qt 6 migration is not validated.')
    native = 'arm64' if platform.machine() == 'arm64' else 'x86_64'
    if args.arch != native:
        raise SystemExit('Build/test natively on the requested CPU architecture; no implicit Rosetta or universal build.')
    if args.distribution and (not args.sign_identity or not args.notary_profile):
        raise SystemExit('Distribution builds require both --sign-identity (Developer ID Application) and --notary-profile. Refusing to produce an unsigned/unnotarized customer package.')
    if args.notary_profile and not args.sign_identity:
        raise SystemExit('--notary-profile requires --sign-identity')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = ROOT/'artifacts'/args.sku/args.arch/stamp
    out.mkdir(parents=True, exist_ok=False)
    work = ROOT/'build'/args.sku/args.arch/stamp
    source = work/'source'
    shutil.copytree(ROOT/('source-'+args.sku), source)
    # The old upstream source uses a case-only VERSION resource which collides
    # on the case-insensitive build volume used by the supported macOS runner.
    version_marker = source/'Software/VERSION'
    if version_marker.exists():
        version_marker.rename(source/'Software/RCX_VERSION.txt')
    # Avoid a hard link against a private Apple class that is absent from public
    # SDK headers. Resolve it dynamically so SDK builds remain linkable.
    mac_utils = source/'Software/grab/MacUtils.mm'
    if mac_utils.exists():
        text = mac_utils.read_text()
        text = text.replace('[[CBBlueLightClient alloc]', '[[NSClassFromString(@"CBBlueLightClient") alloc]')
        text = text.replace('[CBBlueLightClient supportsBlueLightReduction]', '[NSClassFromString(@"CBBlueLightClient") supportsBlueLightReduction]')
        mac_utils.write_text(text)
    env = os.environ.copy()
    env.update(GOFLAGS='-buildvcs=false', GOOS='darwin', GOARCH='arm64' if args.arch=='arm64' else 'amd64', CGO_ENABLED='0')
    env['GOCACHE'] = str(ROOT/'cache/go-macos'/args.arch)
    env['GOPATH'] = str(ROOT/'cache/gopath-macos')
    env['PATH'] = str(qt/'bin') + os.pathsep + env.get('PATH','')
    product = 'RCX Ambient Light Premium' if args.sku=='premium' else 'RCX Ambient Light'
    appversion = '2.9.0' if args.sku=='premium' else '2.8.0'
    identity = 'com.rcx.ambientlight.'+args.sku
    qargs = ['CONFIG+=release', 'CONFIG-=debug', 'QMAKE_APPLE_DEVICE_ARCHS='+args.arch,
             'QMAKE_MACOSX_DEPLOYMENT_TARGET='+args.minimum]
    # Build in a fresh working copy: source snapshots, Windows sources and output
    # packages never receive qmake-generated files or translated resources.
    for directory, project in [('math','math.pro'), ('grab','grab.pro'), ('src','src.pro')]:
        cwd = source/'Software'/directory
        run([qmake, project, *qargs], cwd, env)
        run(['make', '-j'+str(args.jobs)], cwd, env)
    cwd = source/'RCXDesktop'
    run([qmake, 'RCXDesktop.pro', *qargs], cwd, env)
    run(['make', '-j'+str(args.jobs)], cwd, env)
    nativebins = {}
    for pro, binary in [('RCXPlatform.pro','rcx-platform'), ('RCXLauncher.pro','RCX_Launcher')]:
        cwd = work/binary
        cwd.mkdir()
        run([qmake, ROOT/'native'/pro, *qargs, 'DESTDIR='+str(cwd)], cwd, env)
        run(['make', '-j'+str(args.jobs)], cwd, env)
        nativebins[binary] = cwd/binary
    smoke = work/'smoke'
    smoke.mkdir()
    run([qmake, ROOT/'native/RCXSmoke.pro', 'RCX_SOURCE='+str(source), *qargs],smoke,env)
    run(['make','-j'+str(args.jobs)],smoke,env)
    run([smoke/'rcx-smoke','-o',out/'native-smoke-tests.txt,txt'],smoke,env)
    run(['go','test','-v','./...'], source/'RCXSetup', env)
    backend = work/'rcx-backend'
    run(['go','build','-trimpath','-ldflags=-X main.productEdition='+args.sku.title(),'-o',backend,'.'], source/'RCXSetup',env)
    app = out/(product+'.app')
    mac = app/'Contents/MacOS'
    res = app/'Contents/Resources'
    mac.mkdir(parents=True)
    res.mkdir()
    (res/'product-name.txt').write_text(product+'\n')
    run([backend,'--preset-tool','export',res/'RCX_Presets.json'],work,env)
    files = {
        'RCX_Prismatik': source/'Software/bin/RCX_Prismatik.app/Contents/MacOS/RCX_Prismatik',
        'RCX_Configuration': source/'RCXDesktop/RCX_Configuration',
        'rcx-backend': backend,
        **nativebins,
    }
    for name, src in files.items():
        shutil.copy2(src,mac/name)
        (mac/name).chmod(0o755)
        run(['lipo',mac/name,'-verify_arch',args.arch],work,env)
    shutil.copy2(source/'LICENSE',res/'COPYING.txt')
    shutil.copy2(source/'Software/res/icons/Prismatik.icns',res/'Prismatik.icns')
    shutil.copy2(ROOT/'README_ID.md',res/'README_ID.md')
    # Matching source travels with every DMG for GPL compliance and review.
    source_package = res/'Source'
    source_package.mkdir()
    shutil.copytree(source, source_package/('source-'+args.sku), ignore=shutil.ignore_patterns(
        'stuff','bin','lib','RCXFirmware','Makefile*','.qmake.stash','*.o','*.a','*.app','RCX_Configuration','*.qm'))
    shutil.copytree(ROOT/'native',source_package/'native')
    for script in ['build_macos.py','platform_macos.go','README_ID.md','MAC_TEST_CHECKLIST.md']:
        shutil.copy2(ROOT/script,source_package/script)
    notices = ROOT/('source-'+args.sku)/'RCXBuild'
    shutil.copy2(notices/'THIRD_PARTY_NOTICES.md',res/'THIRD_PARTY_NOTICES.md')
    shutil.copytree(notices/'licenses',res/'licenses')
    info = {
        'CFBundleName':product,'CFBundleDisplayName':product,'CFBundleExecutable':'RCX_Launcher',
        'CFBundleIdentifier':identity,'CFBundlePackageType':'APPL','CFBundleShortVersionString':appversion,
        'CFBundleVersion':appversion+'.1','CFBundleIconFile':'Prismatik.icns',
        'LSMinimumSystemVersion':args.minimum,'LSUIElement':True,'NSHighResolutionCapable':True,
        'NSMicrophoneUsageDescription':'RCX uses the selected audio input for optional sound visualization.',
        'NSScreenCaptureUsageDescription':'RCX samples screen colors to control ambient lighting.',
    }
    with (app/'Contents/Info.plist').open('wb') as f: plistlib.dump(info,f)
    run([deploy,app,'-always-overwrite', *['-executable='+str(mac/x) for x in ['RCX_Prismatik','RCX_Configuration','rcx-platform']]],work,env)
    # Sign every nested Mach-O before sealing the outer app. No --deep signing.
    signing = args.sign_identity or '-'
    for f in sorted(app.rglob('*'), key=lambda p:len(p.parts), reverse=True):
        # The CFBundleExecutable is sealed with the app bundle below; sign all
        # other nested Mach-O code first to keep the bundle seal valid.
        if f == mac/'RCX_Launcher': continue
        if not f.is_file() or f.is_symlink():continue
        kind = subprocess.check_output(['file','-b',str(f)],text=True)
        if 'Mach-O' in kind:
            options = ['--timestamp','--options','runtime'] if args.sign_identity else []
            run(['codesign','--force','--sign',signing,*options,f],work,env)
    # Qt frameworks are bundles and need their resources sealed as well.
    for framework in sorted(app.rglob('*.framework'),key=lambda p:len(p.parts),reverse=True):
        run(['codesign','--force','--sign',signing,*(['--timestamp','--options','runtime'] if args.sign_identity else []),framework],work,env)
    run(['codesign','--force','--sign',signing,*(['--timestamp','--options','runtime'] if args.sign_identity else []),app],work,env)
    run(['codesign','--verify','--deep','--strict','--verbose=2',app],work,env)
    if args.notary_profile:
        archive=out/'notarization.zip'
        run(['ditto','-c','-k','--keepParent',app,archive],work,env)
        run(['xcrun','notarytool','submit',archive,'--keychain-profile',args.notary_profile,'--wait'],work,env)
        run(['xcrun','stapler','staple',app],work,env)
        run(['xcrun','stapler','validate',app],work,env)
    stage = work/'dmg'
    stage.mkdir()
    shutil.copytree(app,stage/app.name,symlinks=True)
    (stage/'Applications').symlink_to('/Applications')
    shutil.copy2(ROOT/'README_ID.md',stage/'README_ID.md')
    dmg = out/f'RCX-{args.sku}-{appversion}-macOS-{args.arch}-candidate.dmg'
    run(['hdiutil','create','-volname',product,'-srcfolder',stage,'-format','UDZO',dmg],work,env)
    run(['hdiutil','verify',dmg],work,env)
    if args.notary_profile:
        run(['codesign','--sign',args.sign_identity,'--timestamp',dmg],work,env)
        run(['xcrun','notarytool','submit',dmg,'--keychain-profile',args.notary_profile,'--wait'],work,env)
        run(['xcrun','stapler','staple',dmg],work,env)
        run(['xcrun','stapler','validate',dmg],work,env)
        run(['spctl','--assess','--type','execute','--verbose=2',app],work,env)
    if args.distribution:
        # A distribution build is complete only after Apple's ticket has been
        # stapled to both the app and its delivery image and Gatekeeper accepts it.
        run(['codesign','--verify','--deep','--strict','--verbose=2',app],work,env)
        run(['xcrun','stapler','validate',dmg],work,env)
        run(['spctl','--assess','--type','open','--context','context:primary-signature','--verbose=2',dmg],work,env)
    (out/'SHA256SUMS.txt').write_text(hashlib.sha256(dmg.read_bytes()).hexdigest()+'  '+dmg.name+'\n')
    (out/'BUILD_STATUS.json').write_text(json.dumps({
        'sku':args.sku,'version':appversion,'architecture':args.arch,'qt':version,
        'minimum':args.minimum,'signed':bool(args.sign_identity),'notarized':bool(args.notary_profile),
        'distributionReady':bool(args.distribution and args.sign_identity and args.notary_profile),
        'runtimeHardwareQA':'PENDING: complete MAC_TEST_CHECKLIST.md before distribution',
        'productionRelease':False,
    },indent=2)+'\n')
    print('macOS CANDIDATE (hardware/capture/audio QA still required):',dmg)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sku',choices=['budget','premium'],required=True)
    p.add_argument('--arch',choices=['x86_64','arm64'],required=True)
    p.add_argument('--qt',required=True)
    p.add_argument('--minimum',default='12.0')
    p.add_argument('--jobs',type=int,default=4)
    p.add_argument('--sign-identity')
    p.add_argument('--notary-profile')
    p.add_argument('--distribution',action='store_true',help='Require Developer ID signing and Apple notarization before producing a deliverable DMG.')
    build(p.parse_args())
