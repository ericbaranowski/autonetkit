"""Module to build overlay graphs for network design"""
import autonetkit
import autonetkit.anm
import autonetkit.ank_messaging as ank_messaging
import autonetkit.config
SETTINGS = autonetkit.config.settings
import autonetkit.log as log
import autonetkit.load.graphml as graphml
import autonetkit.exception
import networkx as nx
import autonetkit.ank as ank_utils
import itertools

__all__ = ['build']

MESSAGING = ank_messaging.AnkMessaging()


def build(input_graph_string, timestamp):
    anm = autonetkit.anm.AbstractNetworkModel()

    try:
        input_graph = graphml.load_graphml(input_graph_string)
    except autonetkit.exception.AnkIncorrectFileFormat:
# try a different reader
        try:
            from autonetkit_cisco import load as cisco_load
        except ImportError:
            return  # module not present (development module)
        input_graph = cisco_load.load(input_graph_string)
# add local deployment host
        SETTINGS['General']['deploy'] = True
        SETTINGS['Deploy Hosts']['internal'] = {
            'cisco': {
            'deploy': True,
            },
        }

    input_undirected = nx.Graph(input_graph)
    g_in = anm.add_overlay("input", graph=input_undirected)
    g_in_directed = anm.add_overlay(
        "input_directed", graph=input_graph, directed=True)

# set defaults
    if not g_in.data.specified_int_names:
        # if not specified then automatically assign interface names
        g_in.data.specified_int_names = False

    import autonetkit.plugins.graph_product as graph_product
    graph_product.expand(g_in)  # apply graph products if relevant

    expand_fqdn = False
    # TODO: make this set from config and also in the input file
    if expand_fqdn and len(ank_utils.unique_attr(g_in, "asn")) > 1:
        # Multiple ASNs set, use label format device.asn
        anm.set_node_label(".", ['label', 'pop', 'asn'])

    g_in.update(
        g_in.nodes("is_router", platform="junosphere"), syntax="junos")
    g_in.update(g_in.nodes("is_router", platform="dynagen"), syntax="ios")
    g_in.update(g_in.nodes("is_router", platform="netkit"), syntax="quagga")

    g_graphics = anm.add_overlay("graphics")  # plotting data
    g_graphics.add_nodes_from(g_in, retain=['x', 'y', 'device_type',
                              'device_subtype', 'pop', 'asn'])

    build_phy(anm)
    g_phy = anm['phy']

    build_vrf(anm)

    address_family = g_in.data.address_family or "v4"
    allocate_ipv4_infrastructure = False
    if address_family in ("v4", "dual_stack"):
        allocate_ipv4_infrastructure = True
    build_ipv4(anm, infrastructure=allocate_ipv4_infrastructure)

    #TODO: Create a collision domain overlay for ip addressing - l2 overlay?
    allocate_ipv6 = False
    if address_family in ("v6", "dual_stack"):
        allocate_ipv6 = True
        build_ipv4(anm, infrastructure=True)

    build_ip6(anm)
    for node in g_phy:
        node.use_ipv4 = allocate_ipv4_infrastructure
        node.use_ipv6 = allocate_ipv6

    igp = g_in.data.igp or "ospf" 
    anm.add_overlay("ospf")
    anm.add_overlay("isis")

    ank_utils.copy_attr_from(g_in, g_phy, "include_csr")

    if igp == "ospf":
        build_ospf(anm)
    if igp == "isis":
        build_isis(anm)
    build_bgp(anm)

    return anm

def build_vrf(anm):
    g_in = anm['input']
    g_phy = anm['phy']
    g_vrf = anm.add_overlay("vrf", directed=True)
    g_vrf.add_nodes_from(g_in.nodes("is_router"), retain=["vrf_role", "vrf"])

    for node in g_vrf.nodes(vrf_role="CE"):
        if not node.vrf:
            node.vrf = "default_vrf"

    for node in g_vrf.nodes('vrf'):
        node.vrf_role = "CE"

    non_ce_nodes = [n for n in g_vrf if n.vrf_role != "CE"]
    for n in non_ce_nodes:
        phy_neighbors = g_phy.node(
            n).neighbors()  # neighbors from physical graph for connectivity
        phy_neighbors = [
            neigh for neigh in phy_neighbors if neigh.asn == n.asn]
            # filter to just this asn
        if any(g_vrf.node(neigh).vrf_role == "CE" for neigh in phy_neighbors):
            # phy neigh has vrf set in this graph
            n.vrf_role = "PE"
        else:
            n.vrf_role = "P"  # default role

# add links from CE to PE
    for _, devices in g_phy.groupby("asn").items():
        as_graph = g_phy.subgraph(devices)
        edges = [(edge.src, edge.dst) for edge in as_graph.edges()]
        vrf_edges = [(g_vrf.node(s), g_vrf.node(t)) for (s, t) in edges]
        pe_to_ce_edges = []
        ce_to_pe_edges = []
        for s, t in vrf_edges:
            if (s.vrf_role, t.vrf_role) == ("CE", "PE"):
                pe_to_ce_edges.append((s, t))
                ce_to_pe_edges.append((t, s))
            if (s.vrf_role, t.vrf_role) == ("PE", "CE"):
                pe_to_ce_edges.append((t, s))
                ce_to_pe_edges.append((s, t))
# other direction

        g_vrf.add_edges_from(pe_to_ce_edges, direction="u")
        g_vrf.add_edges_from(ce_to_pe_edges, direction="d")

# and map to create extra loopbacks
    for node in g_vrf.nodes(vrf_role="PE"):
        node_vrf_names = {n.vrf for n in node.neighbors(vrf_role="CE")}
        node.node_vrf_names = node_vrf_names
        node.rd_indices = {}
        for index, vrf_name in enumerate(node_vrf_names, 1):
            node.rd_indices[vrf_name] = index
            node.add_loopback(vrf_name=vrf_name,
                              description="loopback for vrf %s" % vrf_name)

    for node in g_vrf.nodes(vrf_role="CE"):
        node.add_loopback(vrf_name=n.vrf_name,
                          description="loopback for vrf %s" % n.vrf_name)

    # allocate route-targets per AS
    # This could later look at connected components for each ASN
    route_targets = {}
    for asn, devices in ank_utils.groupby("asn", g_vrf.nodes(vrf_role = "PE")):
        asn_vrfs = [d.node_vrf_names for d in devices]
        # flatten list to unique set
        asn_vrfs = set(itertools.chain.from_iterable(asn_vrfs)) 
        route_targets[asn] = {vrf: "%s:%s" % (asn, index)
                for index, vrf in enumerate(sorted(asn_vrfs), 1)}

    g_vrf.data.route_targets = route_targets

    for node in g_vrf:
        vrf_loopbacks = node.interfaces("is_loopback", "vrf_name")
        for index, interface in enumerate(vrf_loopbacks, start = 101):
            interface.index = index 

# Create route-targets


def three_tier_ibgp_l1_l3_clusters(rtrs):
    up_links = []
    down_links = []
    for l3_cluster, l3d in ank_utils.groupby("ibgp_l3_cluster", rtrs):
        for l2_cluster, l2d in ank_utils.groupby("ibgp_l2_cluster", l3d):
            l2d = list(l2d)
            if any(r.ibgp_level == 2 for r in l2d):
                log.debug("Cluster (%s, %s) has l2 devices, not "
                          "adding extra links" % (l3_cluster, l2_cluster))
            else:
                l1_rtrs = [r for r in l2d if r.ibgp_level == 1]
                l3_rtrs = [r for r in l2d if r.ibgp_level == 3]
                if not(len(l1_rtrs) and len(l3_rtrs)):
                    break  # no routers to connect
                log.debug("Cluster (%s, %s) has no level 2 iBGP routers."
                          "Connecting l1 routers (%s) to l3 routers (%s)"
                          % (l3_cluster, l2_cluster, l1_rtrs, l3_rtrs))

                l1_l3_up_links = [(s, t) for s in l1_rtrs for t in l3_rtrs]
                up_links += l1_l3_up_links
                down_links += [(t, s) for (s, t) in l1_l3_up_links]

    return up_links, down_links



def three_tier_ibgp_edges(routers):
    """Constructs three-tier ibgp"""
    up_links = []
    down_links = []
    over_links = []
    all_pairs = [(s, t) for s in routers for t in routers if s != t]
    l1_l2_up_links = [(s, t) for (s, t) in all_pairs
                      if (s.ibgp_level, t.ibgp_level) == (1, 2)
                      and s.ibgp_l2_cluster == t.ibgp_l2_cluster
                      and s.ibgp_l3_cluster == t.ibgp_l3_cluster
                      ]
    up_links += l1_l2_up_links
    down_links += [(t, s) for (s, t) in l1_l2_up_links]  # the reverse

    over_links += [(s, t) for (s, t) in all_pairs
                   if s.ibgp_level == t.ibgp_level == 2
                   and s.ibgp_l2_cluster == t.ibgp_l2_cluster
                   and s.ibgp_l3_cluster == t.ibgp_l3_cluster
                   ]  # l2 peer links

    l2_l3_up_links = [(s, t) for (s, t) in all_pairs
                      if (s.ibgp_level, t.ibgp_level) == (2, 3)
                      and s.ibgp_l3_cluster == t.ibgp_l3_cluster]
    up_links += l2_l3_up_links
    down_links += [(t, s) for (s, t) in l2_l3_up_links]  # the reverse

    over_links += [(s, t) for (s, t) in all_pairs
                   if s.ibgp_level == t.ibgp_level == 3]  # l3 peer links

# also check for any clusters which only contain l1 and l3 links
    l1_l3_up_links, l1_l3_down_links = three_tier_ibgp_l1_l3_clusters(routers)
    up_links += l1_l3_up_links
    down_links += l1_l3_down_links
  
    return up_links, down_links, over_links


def build_two_tier_ibgp(routers):
    """Constructs two-tier ibgp"""
    up_links = down_links = over_links = []
    all_pairs = [(s, t) for s in routers for t in routers if s != t]
    up_links = [(s, t) for (s, t) in all_pairs
                if (s.ibgp_level, t.ibgp_level) == (1, 2)
                and s.ibgp_l3_cluster == t.ibgp_l3_cluster]
    down_links = [(t, s) for (s, t) in up_links]  # the reverse

    over_links = [(s, t) for (s, t) in all_pairs
                  if s.ibgp_level == t.ibgp_level == 2
                  and s.ibgp_l3_cluster == t.ibgp_l3_cluster]
    return up_links, down_links, over_links


def build_bgp(anm):
    """Build iBGP end eBGP overlays"""
    # eBGP
    g_phy = anm['phy']
    g_in = anm['input']
    g_bgp = anm.add_overlay("bgp", directed=True)
    g_bgp.add_nodes_from(g_in.nodes("is_router"))
    ebgp_edges = [edge for edge in g_in.edges() if not edge.attr_equal("asn")]
    g_bgp.add_edges_from(ebgp_edges, bidirectional=True, type='ebgp')

# now iBGP
    ank_utils.copy_attr_from(g_in, g_bgp, "ibgp_level")
    ank_utils.copy_attr_from(g_in, g_bgp, "ibgp_l2_cluster")
    ank_utils.copy_attr_from(g_in, g_bgp, "ibgp_l3_cluster")
    for node in g_bgp:
        # set defaults
        if node.ibgp_level is None:
            node.ibgp_level = 1

        if node.ibgp_level == "None":  # if unicode string from yEd
            node.ibgp_level = 1

        node.ibgp_level = int(node.ibgp_level)  # ensure is numeric

        if not node.ibgp_l2_cluster or node.ibgp_l2_cluster == "None":
            # ibgp_l2_cluster defaults to region
            node.ibgp_l2_cluster = node.region or "default_l2_cluster"
        if not node.ibgp_l3_cluster or node.ibgp_l3_cluster == "None":
            # ibgp_l3_cluster defaults to ASN
            node.ibgp_l3_cluster = node.asn

    for _, devices in ank_utils.groupby("asn", g_bgp):
        # group by nodes in phy graph
        routers = list(g_bgp.node(n) for n in devices if n.is_router)
        # list of nodes from bgp graph
        ibgp_levels = {int(r.ibgp_level) for r in routers}
        max_level = max(ibgp_levels)
        # all possible edge src/dst pairs
        all_pairs = [(s, t) for s in routers for t in routers if s != t]
        if max_level == 3:
            up_links, down_links, over_links = three_tier_ibgp_edges(routers)

        if max_level == 2:
            up_links, down_links, over_links = build_two_tier_ibgp(routers)

        elif max_level == 1:
            up_links = []
            down_links = []
            over_links = [(s, t) for (s, t) in all_pairs
                             if s.ibgp_l3_cluster == t.ibgp_l3_cluster]

        g_bgp.add_edges_from(up_links, type='ibgp', direction='up')
        g_bgp.add_edges_from(down_links, type='ibgp', direction='down')
        g_bgp.add_edges_from(over_links, type='ibgp', direction='over')

# and set label back
    ibgp_label_to_level = {
        0: "None",  # Explicitly set role to "None" -> Not in iBGP
        3: "RR",
        1: "RRC",
        2: "HRR",
    }
    for node in g_bgp:
        node.ibgp_role = ibgp_label_to_level[node.ibgp_level]

    ebgp_nodes = [d for d in g_bgp if any(
        edge.type == 'ebgp' for edge in d.edges())]
    g_bgp.update(ebgp_nodes, ebgp=True)

    for edge in g_bgp.edges(type='ibgp'):
        # TODO: need interface querying/selection. rather than hard-coded ids
        edge.bind_interface(edge.src, 0)

    #for node in g_bgp:
        #node._interfaces[0]['description'] = "loopback0"


def build_ip6(anm):
    """Builds IPv6 graph, using nodes and edges from IPv4 graph"""
    import autonetkit.plugins.ipv6 as ipv6
    # uses the nodes and edges from ipv4
    g_ipv6 = anm.add_overlay("ipv6")
    g_ipv4 = anm['ipv4']
    g_ipv6.add_nodes_from(
        g_ipv4, retain="collision_domain")  # retain if collision domain or not
    g_ipv6.add_edges_from(g_ipv4.edges())
    ipv6.allocate_ips(g_ipv6)


def manual_ipv4_allocation(anm):
    """Applies manual IPv4 allocation"""
    import netaddr
    g_in_directed = anm['input_directed']
    g_ipv4 = anm['ipv4']

    for l3_device in g_ipv4.nodes("is_l3device"):
        directed_node = g_in_directed.node(l3_device)
        l3_device.loopback = directed_node.ipv4loopback
        for edge in l3_device.edges():
            # find edge in g_in_directed
            directed_edge = g_in_directed.edge(edge)
            edge.ip_address = netaddr.IPAddress(directed_edge.ipv4)

            # set subnet onto collision domain (can come from either
            # direction)
            collision_domain = edge.dst
            if not collision_domain.subnet:
                # TODO: see if direct method in netaddr to deduce network
                prefixlen = directed_edge.netPrefixLenV4
                cidr_string = "%s/%s" % (edge.ip_address, prefixlen)

                intermediate_subnet = netaddr.IPNetwork(cidr_string)
                cidr_string = "%s/%s" % (
                    intermediate_subnet.network, prefixlen)
                subnet = netaddr.IPNetwork(cidr_string)
                collision_domain.subnet = subnet

    # also need to form aggregated IP blocks (used for e.g. routing prefix
    # advertisement)
    loopback_blocks = {}
    infra_blocks = {}
    for asn, devices in g_ipv4.groupby("asn").items():
        routers = [d for d in devices if d.is_router]
        loopbacks = [r.loopback for r in routers]
        loopback_blocks[asn] = netaddr.cidr_merge(loopbacks)

        collision_domains = [d for d in devices if d.collision_domain]
        subnets = [cd.subnet for cd in collision_domains]
        infra_blocks[asn] = netaddr.cidr_merge(subnets)

    g_ipv4.data.loopback_blocks = loopback_blocks
    g_ipv4.data.infra_blocks = infra_blocks


def build_ipv4(anm, infrastructure=True):
    """Builds IPv4 graph"""
    g_ipv4 = anm.add_overlay("ipv4")
    g_in = anm['input']
    g_graphics = anm['graphics']
    g_phy = anm['phy']

    g_ipv4.add_nodes_from(g_in)
    g_ipv4.add_edges_from(g_in.edges(type="physical"))

    ank_utils.aggregate_nodes(g_ipv4, g_ipv4.nodes("is_switch"),
                              retain="edge_id")

    edges_to_split = [edge for edge in g_ipv4.edges() if edge.attr_both(
        "is_l3device")]
    split_created_nodes = list(
        ank_utils.split(g_ipv4, edges_to_split, retain='edge_id'))
    for node in split_created_nodes:
        node['graphics'].x = ank_utils.neigh_average(g_ipv4, node, "x",
                                                     g_graphics)
        node['graphics'].y = ank_utils.neigh_average(g_ipv4, node, "y",
                                                     g_graphics)
        asn = ank_utils.neigh_most_frequent(
            g_ipv4, node, "asn", g_phy)  # arbitrary choice
        node['graphics'].asn = asn
        node.asn = asn # need to use asn in IP overlay for aggregating subnets

    switch_nodes = g_ipv4.nodes("is_switch")  # regenerate due to aggregated
    g_ipv4.update(switch_nodes, collision_domain=True)
                 # switches are part of collision domain
    g_ipv4.update(split_created_nodes, collision_domain=True)
# Assign collision domain to a host if all neighbours from same host
    for node in split_created_nodes:
        if ank_utils.neigh_equal(g_ipv4, node, "host", g_phy):
            node.host = ank_utils.neigh_attr(
                g_ipv4, node, "host", g_phy).next()  # first attribute

# set collision domain IPs
    for node in g_ipv4.nodes("collision_domain"):
        graphics_node = g_graphics.node(node)
        graphics_node.device_type = "collision_domain"
        if not node.is_switch:
            label = "_".join(
                sorted(ank_utils.neigh_attr(g_ipv4, node, "label", g_phy)))
            cd_label = "cd_%s" % label  # switches keep their names
            node.label = cd_label
            node.cd_id = cd_label
            graphics_node.label = cd_label

    #TODO: need to set allocate_ipv4 by default in the readers
    if g_in.data.allocate_ipv4 is False:
        manual_ipv4_allocation(anm)
    else:
        import autonetkit.plugins.ipv4 as ipv4
        ipv4.allocate_ips(g_ipv4, infrastructure)
        ank_utils.save(g_ipv4)


def build_phy(anm):
    """Build physical overlay"""
    g_in = anm['input']
    g_phy = anm['phy']
    g_phy.add_nodes_from(g_in, retain=['label', 'update', 'device_type', 'asn',
                         'device_subtype', 'platform', 'host', 'syntax'])
    if g_in.data.Creator == "Topology Zoo Toolset":
        ank_utils.copy_attr_from(g_in, g_phy, "Network")

    g_phy.add_edges_from(g_in.edges(type="physical"))
    g_phy.allocate_interfaces(
    )  # TODO: make this automatic if adding to the physical graph?


def build_conn(anm):
    """Build connectivity overlay"""
    g_in = anm['input']
    g_conn = anm.add_overlay("conn", directed=True)
    g_conn.add_nodes_from(g_in, retain=['label'])
    g_conn.add_edges_from(g_in.edges(type="physical"))

    return


def build_ospf(anm):
    """
    Build OSPF graph.

    Allowable area combinations:
    0 -> 0
    0 -> x (x!= 0)
    x -> 0 (x!= 0)
    x -> x (x != 0)

    Not-allowed:
    x -> x (x != y != 0)
    """
    import netaddr
    g_in = anm['input']
    g_ospf = anm.add_overlay("ospf")
    g_ospf.add_nodes_from(g_in.nodes("is_router"), retain=['asn'])
    g_ospf.add_nodes_from(g_in.nodes("is_switch"), retain=['asn'])
    g_ospf.add_edges_from(g_in.edges(), retain=['edge_id'])

    ank_utils.copy_attr_from(g_in, g_ospf, "ospf_area", dst_attr="area")
    ank_utils.copy_edge_attr_from(g_in, g_ospf, "ospf_cost", dst_attr="cost")

    ank_utils.aggregate_nodes(g_ospf, g_ospf.nodes("is_switch"),
                              retain="edge_id")
    ank_utils.explode_nodes(g_ospf, g_ospf.nodes("is_switch"),
                            retain="edge_id")

    g_ospf.remove_edges_from([link for link in g_ospf.edges(
    ) if link.src.asn != link.dst.asn])  # remove inter-AS links

    area_zero_ip = netaddr.IPAddress("0.0.0.0")
    area_zero_int = 0
    area_zero_ids = {area_zero_ip, area_zero_int}
    default_area = area_zero_int
    if any(router.area == "0.0.0.0" for router in g_ospf):
        # string comparison as hasn't yet been cast to IPAddress
        default_area = area_zero_ip

    for router in g_ospf:
        if not router.area or router.area == "None":
            router.area = default_area
            # check if 0.0.0.0 used anywhere, if so then use 0.0.0.0 as format
        else:
            try:
                router.area = int(router.area)
            except ValueError:
                try:
                    router.area = netaddr.IPAddress(router.area)
                except netaddr.core.AddrFormatError:
                    log.warning("Invalid OSPF area %s for %s. Using default"
                                " of %s" % (router.area, router, default_area))
                    router.area = default_area

    for router in g_ospf:
# and set area on interface
        for edge in router.edges():
            if edge.area:
                continue  # allocated (from other "direction", as undirected)
            if router.area == edge.dst.area:
                edge.area = router.area  # intra-area
                continue

            if router.area in area_zero_ids or edge.dst.area in area_zero_ids:
# backbone to other area
                if router.area in area_zero_ids:
                    # router in backbone, use other area
                    edge.area = edge.dst.area
                else:
                    # router not in backbone, use its area
                    edge.area = router.area

    for router in g_ospf:
        areas = {edge.area for edge in router.edges()}

        router.areas = list(
            areas)  # store all the edges a router participates in

        if len(areas) in area_zero_ids:
            router.type = "backbone"  # no ospf edges (eg single node in AS)
        elif len(areas) == 1:
            # single area: either backbone (all 0) or internal (all nonzero)
            if len(areas & area_zero_ids):
                # intersection has at least one element -> router has area zero
                router.type = "backbone"
            else:
                router.type = "internal"

        else:
            # multiple areas
            if len(areas & area_zero_ids):
                # intersection has at least one element -> router has area zero
                router.type = "backbone ABR"
            else:
                log.warning(
                    "%s spans multiple areas but is not a member of area 0"
                    % router)
                router.type = "INVALID"

    if (any(area_zero_int in router.areas for router in g_ospf) and
            any(area_zero_ip in router.areas for router in g_ospf)):
        log.warning("Using both area 0 and area 0.0.0.0")

    for link in g_ospf.edges():
        if not link.cost:
            link.cost = 1


def ip_to_net_ent_title_ios(ip):
    """ Converts an IP address into an OSI Network Entity Title
    suitable for use in IS-IS on IOS.

    >>> ip_to_net_ent_title_ios(IPAddress("192.168.19.1"))
    '49.1921.6801.9001.00'
    """
    try:
        ip_words = ip.words
    except AttributeError:
        import netaddr  # try to cast to IP Address
        ip = netaddr.IPAddress(ip)
        ip_words = ip.words

    log.debug("Converting IP to OSI ENT format")
    area_id = "49"
    ip_octets = "".join("%03d" % int(
        octet) for octet in ip_words)  # single string, padded if needed
    return ".".join([area_id, ip_octets[0:4], ip_octets[4:8], ip_octets[8:12],
                     "00"])


def build_isis(anm):
    """Build isis overlay"""
    g_in = anm['input']
    g_ipv4 = anm['ipv4']
    g_isis = anm.add_overlay("isis")
    g_isis.add_nodes_from(g_in.nodes("is_router"), retain=['asn'])
    g_isis.add_nodes_from(g_in.nodes("is_switch"), retain=['asn'])
    g_isis.add_edges_from(g_in.edges(), retain=['edge_id'])
# Merge and explode switches
    ank_utils.aggregate_nodes(g_isis, g_isis.nodes("is_switch"),
                              retain="edge_id")
    ank_utils.explode_nodes(g_isis, g_isis.nodes("is_switch"),
                            retain="edge_id")

    g_isis.remove_edges_from(
        [link for link in g_isis.edges() if link.src.asn != link.dst.asn])

    for node in g_isis:
        ip_node = g_ipv4.node(node)
        node.net = ip_to_net_ent_title_ios(ip_node.loopback)
        node.process_id = 1  # default

    for link in g_isis.edges():
        link.metric = 1  # default


def update_messaging(anm):
    log.debug("Sending anm to messaging")
    body = autonetkit.ank_json.dumps(anm, None)
    MESSAGING.publish_compressed("www", "client", body)
