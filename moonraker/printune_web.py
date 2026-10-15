# Printune page for Moonraker: serves web/index.html at /server/printune
#
# Install: ./install.sh links this file into ~/moonraker/moonraker/components/
# and adds [printune_web] to moonraker.conf. The file must stay in the Printune
# folder (it reads ../web/index.html from there, so updates need no restart).
#
# Copyright (C) 2026  Printune contributors
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import os

PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                    "web", "index.html")


class PrintuneWeb:
    def __init__(self, config):
        self.server = config.get_server()
        self.page = os.path.expanduser(config.get("page", PAGE))
        # the page itself is public like Mainsail's; its API calls use Moonraker's normal auth
        self.server.register_endpoint(
            "/server/printune", ["GET"], self._handle_page, transports=["http"],
            wrap_result=False, content_type="text/html; charset=UTF-8", auth_required=False)

    async def _handle_page(self, web_request):
        eventloop = self.server.get_event_loop()
        return await eventloop.run_in_thread(self._read)

    def _read(self):
        with open(self.page, "rb") as f:
            return f.read()


def load_component(config):
    return PrintuneWeb(config)
