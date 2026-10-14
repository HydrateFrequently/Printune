# Virtual Kalico printer for fast, interactive testing: real Kalico (klippy),
# Moonraker and Mainsail, talking to a virtual MCU (Kalico's own simulator
# board, patched to talk serial), with a simulated thermal model. Both klippy
# and the MCU run under libfaketime, so the printer runs SPEED times faster
# than real time (heat-up, heat soak and printing alike).
# Laid out like a Pi OS Lite + KIAUH install (user pi, ~/kalico, ~/klippy-env,
# ~/moonraker, ~/printer_data).
#
#   docker build -t virtual-kalico .                       (Debian bookworm, as Pi OS Lite)
#   docker build --build-arg BASE=ubuntu:24.04 -t virtual-kalico .
ARG BASE=debian:bookworm
FROM ${BASE}

# netfix/ holds only .keep on a normal network. Behind a TLS-inspecting proxy
# put the proxy CA (ca.crt) and an apt-setup.sh there; both are removed again
# at the end of this step.
COPY netfix/ /tmp/netfix/
RUN if [ -f /tmp/netfix/apt-setup.sh ]; then bash /tmp/netfix/apt-setup.sh; fi \
 && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates git curl unzip sudo supervisor nginx \
    build-essential faketime libfaketime \
    python3 python3-dev python3-venv libffi-dev \
    libsodium-dev zlib1g-dev libjpeg-dev liblmdb-dev libopenjp2-7 iproute2 \
 && ln -s "$(find /usr/lib -name libfaketimeMT.so.1 | head -1)" /usr/local/lib/libfaketimeMT.so.1 \
 && if [ -f /tmp/netfix/ca.crt ]; then cp /tmp/netfix/ca.crt /usr/local/share/ca-certificates/netfix.crt && update-ca-certificates; fi \
 && rm -rf /var/lib/apt/lists/* /tmp/netfix /etc/apt/apt.conf.d/99proxy \
 && (userdel -r ubuntu 2>/dev/null || true) \
 && useradd -m -u 1000 -s /bin/bash -G dialout,tty pi \
 && echo 'pi ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/pi \
 && rm -f /etc/nginx/sites-enabled/default

USER pi
WORKDIR /home/pi
ARG KALICO_REPO=https://github.com/KalicoCrew/kalico
# Pinned to the commits this was tested with; override to try newer ones.
ARG KALICO_REF=a6796621ef8abb905935a5b5c194a523a1d45a48
RUN git clone -q ${KALICO_REPO} kalico && git -C kalico checkout -q ${KALICO_REF} \
 && python3 -m venv klippy-env \
 && klippy-env/bin/pip install -q -r kalico/scripts/klippy-requirements.txt
# Virtual MCU: a patched copy of Kalico's simulator board (klippy itself
# runs from the untouched ~/kalico).
COPY --chown=pi vmcu/ /home/pi/vmcu/
COPY --chown=pi config/kalico-vmcu.config /tmp/kalico-vmcu.config
RUN cp -r kalico kalico-vmcu && cd kalico-vmcu \
 && python3 /home/pi/vmcu/patch_simulator.py . \
 && cp /tmp/kalico-vmcu.config .config && make olddefconfig >/dev/null \
 && make -j4 >/dev/null && ls -la out/klipper.elf

ARG MOONRAKER_REF=9e676eba6b02661a4dfa3ec6e7ac3f3504498e6d
RUN git clone -q https://github.com/Arksine/moonraker && git -C moonraker checkout -q ${MOONRAKER_REF} \
 && python3 -m venv moonraker-env \
 && moonraker-env/bin/pip install -q -r moonraker/scripts/moonraker-requirements.txt
RUN curl -sSL -o /tmp/mainsail.zip https://github.com/mainsail-crew/mainsail/releases/latest/download/mainsail.zip \
 && mkdir mainsail && unzip -q /tmp/mainsail.zip -d mainsail && rm /tmp/mainsail.zip \
 && git clone -q --depth 1 https://github.com/mainsail-crew/mainsail-config

RUN mkdir -p printer_data/config printer_data/gcodes printer_data/logs printer_data/comms printer_data/database \
 && ln -s /home/pi/mainsail-config/mainsail.cfg printer_data/config/mainsail.cfg
COPY --chown=pi printer_config/ /home/pi/default_config/
RUN ln -s /home/pi/default_config/sim_thermal.py kalico/klippy/extras/sim_thermal.py

USER root
COPY config/nginx.conf /etc/nginx/conf.d/mainsail.conf
COPY config/supervisord.conf /etc/supervisor/supervisord.conf
COPY scripts/start.sh /usr/local/bin/start
RUN chmod +x /usr/local/bin/start && chmod o+x /home/pi
EXPOSE 80 7125
CMD ["/usr/local/bin/start"]
