#!/usr/bin/env python3
"""Run the virtual MCU binary on a pseudo-tty (default /tmp/pseudoserial)."""
import os, pty, sys, termios, tty

def main():
    binary = sys.argv[1]
    link = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pseudoserial"
    mfd, sfd = pty.openpty()
    tty.setraw(mfd)
    tty.setraw(sfd)
    os.set_inheritable(sfd, True)
    name = os.ttyname(sfd)
    try:
        os.unlink(link)
    except OSError:
        pass
    os.symlink(name, link)
    os.dup2(mfd, 0)
    os.dup2(mfd, 1)
    os.execv(binary, [binary])

if __name__ == "__main__":
    main()
