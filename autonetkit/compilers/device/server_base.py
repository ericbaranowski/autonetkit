#!/usr/bin/python
# -*- coding: utf-8 -*-
import autonetkit.log as log
from autonetkit.compilers.device.device_base import DeviceCompiler

class ServerCompiler(DeviceCompiler):

    def compile(self, node):
        #TODO: call this from parent
        node.do_render = True  # turn on rendering
        phy_node = self.anm['phy'].node(node)
        ipv4_node = self.anm['ipv4'].node(node)
        super(ServerCompiler, self).compile(node)
