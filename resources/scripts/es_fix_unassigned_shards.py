
# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

from pyLibrary import convert
from pyLibrary.debugs import constants
from pyLibrary.debugs import startup
from pyLibrary.debugs.logs import Log
from pyLibrary.dot import wrap, listwrap
from pyLibrary.env import http
from pyLibrary.maths import Math
from pyLibrary.maths.randoms import Random
from pyLibrary.queries import jx
from pyLibrary.queries.unique_index import UniqueIndex
from pyLibrary.thread.threads import Thread

CONCURRENT = 3
BIG_SHARD_SIZE = 20 * 1024 * 1024 * 1024  # SIZE WHEN WE SHOULD BE MOVING ONLY ONE SHARD AT A TIME

def assign_shards(settings):
    """
    ASSIGN THE UNASSIGNED SHARDS
    """
    path = settings.elasticsearch.host + ":" + unicode(settings.elasticsearch.port)

    # GET LIST OF NODES
    # coordinator    26.2gb
    # secondary     383.7gb
    # spot_47727B30   934gb
    # spot_BB7A8053   934gb
    # primary       638.8gb
    # spot_A9DB0988     5tb
    Log.note("get nodes")
    nodes = UniqueIndex("name", list(convert_table_to_list(
        http.get(path + "/_cat/nodes?bytes=b&h=n,r,d,i,hm").content,
        ["name", "role", "disk", "ip", "memory"]
    )))
    if "primary" not in nodes or "secondary" not in nodes:
        Log.error("missing an important index\n{{nodes|json}}", nodes=nodes)

    for n in nodes:
        if n.role == 'd':
            n.disk = 0 if n.disk == "" else float(n.disk)
            n.memory = text_to_bytes(n.memory)
        else:
            n.disk = 0
            n.memory = 0

        if n.name.startswith("spot_") or n.name.startswith("coord"):
            n.zone = "spot"
        else:
            n.zone = n.name
    # Log.note("Nodes:\n{{nodes}}", nodes=list(nodes))

    # GET LIST OF SHARDS, WITH STATUS
    # debug20150915_172538                0  p STARTED        37319   9.6mb 172.31.0.196 primary
    # debug20150915_172538                0  r UNASSIGNED
    # debug20150915_172538                1  p STARTED        37624   9.6mb 172.31.0.39  secondary
    # debug20150915_172538                1  r UNASSIGNED
    shards = wrap(list(convert_table_to_list(http.get(path + "/_cat/shards").content,
                                             ["index", "i", "type", "status", "num", "size", "ip", "node"])))
    for s in shards:
        s.i = int(s.i)
        s.size = text_to_bytes(s.size)
        s.zone = nodes[s.node].zone

    # ASSIGN SIZE TO ALL SHARDS
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        replicas = wrap(list(replicas))
        size = max(*replicas.size)
        for r in replicas:
            r.size = size
    for g, replicas in jx.groupby(shards, "index"):
        replicas = wrap(list(replicas))
        index_size = Math.sum(replicas.size)
        for r in replicas:
            r.index_size=index_size

    relocating = [s for s in shards if s.status in ("RELOCATING", "INITIALIZING")]

    # LOOKING FOR SHARDS WITH ZERO INSTANCES, IN THE spot ZONE
    not_started = []
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        replicas = list(replicas)
        started_replicas = list(set([s.zone for s in replicas if s.status == "STARTED"]))
        if len(started_replicas) == 0:
            # MARK NODE AS RISKY
            for s in replicas:
                if s.status == "UNASSIGNED":
                    not_started.append(s)
                    break  # ONLY NEED ONE
    if not_started:
        Log.note("{{num}} shards have not started", num=len(not_started))
        if len(relocating)>1:
            Log.note("Delay work, cluster busy RELOCATING/INITIALIZING {{num}} shards", num=len(relocating))
        else:
            allocate(30, not_started, relocating, path, nodes, set(n.zone for n in nodes) - {"spot"}, shards)
        return
    else:
        Log.note("No not-started shards found")


    # LOOKING FOR SHARDS WITH ONLY ONE INSTANCE, IN THE spot ZONE
    high_risk_shards = []
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        replicas = list(replicas)
        safe_zones = list(set([s.zone for s in replicas if s.status == "STARTED"]))
        if len(safe_zones) == 0 or (len(safe_zones) == 1 and safe_zones[0] == "spot"):
            # MARK NODE AS RISKY
            for s in replicas:
                if s.status == "UNASSIGNED":
                    high_risk_shards.append(s)
                    break  # ONLY NEED ONE
    if high_risk_shards:
        Log.note("{{num}} high risk shards found", num=len(high_risk_shards))
        allocate(50, high_risk_shards, relocating, path, nodes, set(n.zone for n in nodes) - {"spot"}, shards)
        return
    else:
        Log.note("No high risk shards found")

    # LOOK FOR SHARDS WE CAN MOVE TO SPOT
    too_safe_shards = []
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        replicas = wrap(list(replicas))
        safe_replicas = jx.filter(
            replicas,
            {"and": [
                {"eq": {"status": "STARTED"}},
                {"neq": {"zone": "spot"}}
            ]}
        )
        if len(safe_replicas) >= len(replicas):  # RATHER THAN ONE SAFE SHARD, WE ARE ASKING FOR ONE UNSAFE SHARD
            # TAKE THE SHARD ON THE FULLEST NODE
            # node_load = jx.run({
            #     "select": {"name": "size", "value": "size", "aggregate": "sum"},
            #     "from": shards,
            #     "groupby": ["node"],
            #     "where": {"eq": {"index": replicas[0].index}}
            # })

            i = Random.int(len(replicas))
            shard = replicas[i]
            too_safe_shards.append(shard)

    if too_safe_shards:
        Log.note("{{num}} shards can be moved to spot", num=len(too_safe_shards))
        allocate(CONCURRENT, too_safe_shards, relocating, path, nodes, {"spot"}, shards)
        return
    else:
        Log.note("No shards moved")

    # LOOK FOR UNALLOCATED SHARDS WE CAN PUT IN THE SPOT ZONE
    low_risk_shards = []
    for g, replicas in jx.groupby(shards, ["index", "i"]):
        replicas = wrap(list(replicas))
        size = Math.MAX(replicas.size)
        current_zones = list(set([s.zone for s in replicas if s.status == "STARTED"]))
        if "spot" not in current_zones:
            # WE CAN ASSIGN THIS REPLICA TO spot
            for s in replicas:
                if s.status == "UNASSIGNED":
                    s.size = size
                    low_risk_shards.append(s)
                    break  # ONLY NEED ONE

    if low_risk_shards:
        Log.note("{{num}} low risk shards found", num=len(low_risk_shards))

        allocate(CONCURRENT, low_risk_shards, relocating, path, nodes, {"spot"}, shards)
        return
    else:
        Log.note("No low risk shards found")


def net_shards_to_move(concurrent, shards, relocating):
    sorted_shards = jx.sort(shards, ["index_size", "size"])
    size = (sorted_shards[0].size+1) / BIG_SHARD_SIZE   # +1 to avoid divide-by-zero
    concurrent = min(concurrent, Math.ceiling(1 / size))
    net = concurrent - len(relocating)
    return net, sorted_shards


def allocate(concurrent, proposed_shards, relocating, path, nodes, zones, all_shards):
    net, shards = net_shards_to_move(concurrent, proposed_shards, relocating)
    if net <= 0:
        Log.note("Delay work, cluster busy RELOCATING/INITIALIZING {{num}} shards", num=len(relocating))
        return

    for shard in shards:
        if net <= 0:
            break
        # DIVIDE EACH NODE MEMORY BY NUMBER OF SHARDS FROM THIS INDEX
        node_weight = {n.name: n.memory for n in nodes}
        shards_for_this_index = wrap(jx.filter(all_shards, {
            "eq": {
                "index": shard.index,
                "status": "STARTED"
            }
        }))
        index_size = Math.sum(shards_for_this_index.size)
        for g, ss in jx.groupby(shards_for_this_index, "node"):
            ss = wrap(list(ss))
            node_weight[g.node] = nodes[g.node].memory * (1 - Math.sum(ss.size)/index_size)

        list_nodes = list(nodes)
        while True:
            i = Random.weight([node_weight[n.name] if n.zone in zones else 0 for n in list_nodes])
            destination_node = list_nodes[i].name
            for s in all_shards:
                if s.index == shard.index and s.i == shard.i and s.node == destination_node:
                    Log.note("Shard {{shard.index}}:{{shard.i}} already on node {{node}}", shard=shard, node=destination_node)
                    break
            else:
                break

        if shard.status == "UNASSIGNED":
            # destination_node = "secondary"
            command = wrap({"allocate": {
                "index": shard.index,
                "shard": shard.i,
                "node": destination_node,  # nodes[i].name,
                "allow_primary": True
            }})
        else:
            command = wrap({"move":
                {
                    "index": shard.index,
                    "shard": shard.i,
                    "from_node": shard.node,
                    "to_node": destination_node
                }
            })

        result = convert.json2value(
            convert.utf82unicode(http.post(path + "/_cluster/reroute", json={"commands": [command]}).content))
        if not result.acknowledged:
            Log.warning("Can not move/allocate to {{node}}: {{error}}", node=destination_node, error=result.error)
        else:
            net -= 1
            Log.note(
                "index={{shard.index}}, shard={{shard.i}}, assign_to={{node}}, ok={{result.acknowledged}}",
                shard=shard,
                result=result,
                node=destination_node
            )


def convert_table_to_list(table, column_names):
    lines = [l for l in table.split("\n") if l.strip()]

    # FIND THE COLUMNS WITH JUST SPACES
    columns = []
    for i, c in enumerate(zip(*lines)):
        if all(r == " " for r in c):
            columns.append(i)

    for i, row in enumerate(lines):
        yield wrap({c: r for c, r in zip(column_names, split_at(row, columns))})


def split_at(row, columns):
    output = []
    last = 0
    for c in columns:
        output.append(row[last:c].strip())
        last = c
    output.append(row[last:].strip())
    return output


def text_to_bytes(size):
    if size == "":
        return 0

    multiplier = {
        "kb": 1000,
        "mb": 1000000,
        "gb": 1000000000
    }.get(size[-2:])
    if not multiplier:
        multiplier = 1
        if size[-1]=="b":
            size = size[:-1]
    else:
        size = size[:-2]
    try:
        return float(size) * float(multiplier)
    except Exception, e:
        Log.error("not expected", cause=e)


def main():
    settings = startup.read_settings()
    Log.start(settings.debug)

    constants.set(settings.constants)
    path = settings.elasticsearch.host + ":" + unicode(settings.elasticsearch.port)

    try:
        response = http.put(
            path + "/_cluster/settings",
            data='{"persistent": {"cluster.routing.allocation.enable": "none"}}'
        )
        Log.note("DISABLE SHARD MOVEMENT: {{result}}", result=response.all_content)

        while True:
            assign_shards(settings)
            Thread.sleep(seconds=30)
    except Exception, e:
        Log.error("Problem with assign of shards", e)
    finally:
        response = http.put(
            path + "/_cluster/settings",
            data='{"persistent": {"cluster.routing.allocation.enable": "all"}}'
        )
        Log.note("ENABLE SHARD MOVEMENT: {{result}}", result=response.all_content)
        Log.stop()


if __name__ == "__main__":
    main()