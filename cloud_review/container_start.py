"""Prepare the private mounted volume, then irrevocably drop app privileges."""
import os
import runpy
import stat

APP_UID = 65532
APP_GID = 65532


def prepare_and_drop():
    # Only the documented mount point may be modified, never an arbitrary path.
    if os.environ.get('DATA_DIR') != '/data' or os.geteuid() != 0:
        raise RuntimeError('Container startup requires root and DATA_DIR=/data')
    # Refuse symlinks and operate on one opened directory, without recursion.
    descriptor = os.open('/data', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise RuntimeError('Private volume is not a directory')
        os.fchown(descriptor, APP_UID, APP_GID)
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    os.setgroups([])
    os.setgid(APP_GID)
    os.setuid(APP_UID)
    if os.geteuid() != APP_UID or os.getegid() != APP_GID:
        raise RuntimeError('Refusing to run the web app with incorrect privileges')


def main():
    prepare_and_drop()
    print(f'Private volume ready; app uid={APP_UID} gid={APP_GID}', flush=True)
    runpy.run_path('/app/app.py', run_name='__main__')


if __name__ == '__main__':
    main()
