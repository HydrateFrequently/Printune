#!/usr/bin/env python3
"""Turn Kalico's stub 'simulator' board into a usable virtual MCU.

Stock src/simulator compiles but cannot talk to the host: it never reads
serial input and declares no pin names. This patch adds:
  - serial receive: non-blocking reads from stdin fed to serial_rx_byte()
  - pin names PA0..PH7 so printer.cfg can use normal-looking pins
  - input pins read 1 (so '^PIN' endstops report "triggered" at once)
  - a "reset" command (exits; the supervisor restarts it) for FIRMWARE_RESTART
Everything else (steps, PWM, ADC) is accepted and discarded; temperatures
come from the host-side sim_thermal module.
Time is the host's CLOCK_MONOTONIC, so running both this MCU and klippy
under libfaketime with the same speed-up makes the whole printer fast.
Usage: patch_simulator.py <kalico_dir>
"""
import os, sys

src = os.path.join(sys.argv[1], "src", "simulator")


def rep(s, old, new, where):
    """str.replace that fails loudly when upstream Kalico has changed."""
    if old not in s:
        sys.exit("patch_simulator: anchor not found in %s: %r" % (where, old[:60]))
    return s.replace(old, new)


p = os.path.join(src, "gpio.c")
s = open(p).read()
if "DECL_ENUMERATION_RANGE" not in s:
    s = rep(s, '#include "board/gpio.h" // gpio_out_write',
        '#include "board/gpio.h" // gpio_out_write\n'
        '#include "command.h" // DECL_ENUMERATION_RANGE\n\n'
        'DECL_ENUMERATION_RANGE("pin", "PA0", 0, 8);\n'
        'DECL_ENUMERATION_RANGE("pin", "PB0", 8, 8);\n'
        'DECL_ENUMERATION_RANGE("pin", "PC0", 16, 8);\n'
        'DECL_ENUMERATION_RANGE("pin", "PD0", 24, 8);\n'
        'DECL_ENUMERATION_RANGE("pin", "PE0", 32, 8);\n'
        'DECL_ENUMERATION_RANGE("pin", "PF0", 40, 8);\n'
        'DECL_ENUMERATION_RANGE("pin", "PG0", 48, 8);\n'
        'DECL_ENUMERATION_RANGE("pin", "PH0", 56, 8);\n', p)
    s = rep(s, "uint8_t gpio_in_read(struct gpio_in g) {\n    return 0;",
            "uint8_t gpio_in_read(struct gpio_in g) {\n    return 1;", p)
    open(p, "w").write(s)

p = os.path.join(src, "main.c")
s = open(p).read()
if "command_reset" not in s:
    # FIRMWARE_RESTART: exit; supervisord starts a fresh MCU and klippy reconnects
    s = rep(s, '#include "sched.h" // sched_main',
        '#include <stdlib.h> // exit\n#include "command.h" // DECL_COMMAND_FLAGS\n'
        '#include "sched.h" // sched_main\n\n'
        'void\ncommand_reset(uint32_t *args)\n{\n    exit(0);\n}\n'
        'DECL_COMMAND_FLAGS(command_reset, HF_IN_SHUTDOWN, "reset");', p)
    open(p, "w").write(s)

p = os.path.join(src, "serial.c")
s = open(p).read()
if "serial_poll_rx" not in s:
    rep(s, "serial_rx_byte", "", p)  # still uses the generic serial_irq API
    s += '''
// Virtual MCU: poll stdin for host data (called from irq_poll)
void
serial_poll_rx(void)
{
    uint8_t buf[256];
    int ret = read(STDIN_FILENO, buf, sizeof(buf));
    for (int i = 0; i < ret; i++)
        serial_rx_byte(buf[i]);
}
'''
    open(p, "w").write(s)

p = os.path.join(src, "timer.c")
s = open(p).read()
if "serial_poll_rx" not in s:
    s = rep(s, "void\nirq_poll(void)\n{",
            "void serial_poll_rx(void);\n\nvoid\nirq_poll(void)\n{\n    serial_poll_rx();", p)
    # usleep(1) really sleeps ~60 us; under a 10-20x speed-up that makes
    # steps late ("Stepper too far in past"). Busy-poll instead (one core).
    s = rep(s, "    usleep(1);\n", "", p)
    open(p, "w").write(s)
# A virtual MCU drives no hardware, and under a time speed-up the OS
# scheduler's jitter is multiplied too. Late events are harmless here, so
# the real-time sanity shutdowns are turned into no-ops in this build only.
LATE = [
    ("src/sched.c", 'try_shutdown("Timer too close");'),
    ("src/generic/timer_irq.c", 'try_shutdown("Rescheduled timer in the past");'),
    ("src/stepper.c", 'shutdown("Stepper too far in past");'),
    ("src/gpiocmds.c", 'shutdown("Missed scheduling of next digital out event");'),
    ("src/pwmcmds.c", 'shutdown("Missed scheduling of next hard pwm event");'),
]
for rel, call in LATE:
    p = os.path.join(sys.argv[1], rel)
    s = open(p).read()
    if call in s:
        s = s.replace(call, "; /* virtual MCU: ignored */")
        open(p, "w").write(s)
    elif "virtual MCU: ignored" not in s:
        sys.exit("patch_simulator: %s not found in %s" % (call, rel))
print("patched", src)
