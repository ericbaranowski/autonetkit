import autonetkit.log as log
import autonetkit.ank as ank_utils

from autonetkit.ank_utils import call_log

def build_layer2(anm):
        g_l2 = anm.add_overlay('layer2')
        g_phy = anm['phy']
        g_l2.add_nodes_from(g_phy)
        g_l2.add_edges_from(g_phy.edges())
        ank_utils.aggregate_nodes(g_l2, g_l2.switches())

def build_layer2_broadcast(anm):
        g_l2 = anm['layer2']
        g_phy = anm['phy']
        g_graphics = anm['graphics']
        g_l2_bc = anm.add_overlay('layer2_bc')
        g_l2_bc.add_nodes_from(g_l2.l3devices())
        g_l2_bc.add_nodes_from(g_l2.switches())
        g_l2_bc.add_edges_from(g_l2.edges())

        # remove external connectors

        edges_to_split = [edge for edge in g_l2_bc.edges()
            if edge.src.is_l3device() and edge.dst.is_l3device()]
        print "edges to split", edges_to_split
        for edge in edges_to_split:
            edge.split = True  # mark as split for use in building nidb

        split_created_nodes = list(ank_utils.split(g_l2_bc, edges_to_split,
                                   retain=['split'],
                                   id_prepend='cd_'))

        #TODO: if parallel nodes, offset
        #TODO: remove graphics, assign directly
        for node in split_created_nodes:
            node['graphics'].x = ank_utils.neigh_average(g_l2_bc, node, 'x',
                    g_graphics) + 0.1

             # temporary fix for gh-90

            node['graphics'].y = ank_utils.neigh_average(g_l2_bc, node, 'y',
                    g_graphics) + 0.1

                # temporary fix for gh-90

            asn = ank_utils.neigh_most_frequent(g_l2_bc, node, 'asn', g_phy)  # arbitrary choice
            node['graphics'].asn = asn
            node.asn = asn  # need to use asn in IP overlay for aggregating subnets

        from collections import defaultdict
        coincident_nodes = defaultdict(list)
        for node in split_created_nodes:
            coincident_nodes[(node['graphics'].x, node['graphics'].y)].append(node)


        coincident_nodes = {k: v for k, v in coincident_nodes.items()
        if len(v) > 1} # trim out single node co-ordinates
        import math
        for key, val in coincident_nodes.items():
            print "coincidence", val
            for index, item in enumerate(val):
                index = index + 1
                x_offset = 25*math.floor(index/2) * math.pow(-1, index)
                y_offset = -1 * 25*math.floor(index/2) * math.pow(-1, index)
                item['graphics'].x  = item['graphics'].x + x_offset
                item['graphics'].y  = item['graphics'].y + y_offset


        switch_nodes = g_l2_bc.switches()  # regenerate due to aggregated
        g_l2_bc.update(switch_nodes, broadcast_domain=True)

                     # switches are part of collision domain

        for node in split_created_nodes:
            print "npde is", node
            for edge in node.edges():
                print edge

        g_l2_bc.update(split_created_nodes, broadcast_domain=True)

    # Assign collision domain to a host if all neighbours from same host

        for node in split_created_nodes:
            print node, "conncted to", node.neighbors()
            if ank_utils.neigh_equal(g_l2_bc, node, 'host', g_phy):
                node.host = ank_utils.neigh_attr(g_l2_bc, node, 'host',
                        g_phy).next()  # first attribute

        # set collision domain IPs
        #TODO; work out why this throws a json exception
        #autonetkit.ank.set_node_default(g_l2_bc,  broadcast_domain=False)

        for node in g_l2_bc.nodes('broadcast_domain'):
            graphics_node = g_graphics.node(node)
            #graphics_node.device_type = 'broadcast_domain'
            if node.is_switch():
                node['phy'].broadcast_domain = True
            if not node.is_switch():
                # use node sorting, as accomodates for numeric/string names
                graphics_node.device_type = 'broadcast_domain'
                neighbors = sorted(neigh for neigh in node.neighbors())
                label = '_'.join(neigh.label for neigh in neighbors)
                cd_label = 'cd_%s' % label  # switches keep their names
                node.label = cd_label
                graphics_node.label = cd_label
                node.device_type = "broadcast_domain"
                node.label = node.id
                graphics_node.label = node.id


        import autonetkit
        autonetkit.update_http(anm)
        raise SystemExit


def build_layer3(anm):
    """ l3_connectivity graph: switch nodes aggregated and exploded"""
    g_in = anm['input']
    g_l2 = anm['layer2']
    g_l3 = anm.add_overlay("layer3")
    g_l3.add_nodes_from(g_l2 , retain=['label'])
    g_l3.add_nodes_from(g_in.switches(), retain=['asn'])
    g_l3.add_edges_from(g_in.edges())

    ank_utils.aggregate_nodes(g_l3, g_l3.switches())
    exploded_edges = ank_utils.explode_nodes(g_l3,
                                             g_l3.switches())
    for edge in exploded_edges:
        edge.multipoint = True
        edge.src_int.multipoint = True
        edge.dst_int.multipoint = True
