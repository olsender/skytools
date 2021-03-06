#! /usr/bin/env python

## NB: not all commands work ##

"""cascaded queue administration.

londiste.py INI pause [NODE [CONS]]

setadm.py INI pause NODE [CONS]


"""

import sys, time, optparse, skytools

from skytools import UsageError, DBError
from pgq.cascade.nodeinfo import *

__all__ = ['CascadeAdmin']

command_usage = """\
%prog [options] INI CMD [subcmd args]

Node Initialization:
  create-root   NAME CONNSTR
  create-branch NAME CONNSTR --provider=<public_connstr>
  create-leaf   NAME CONNSTR --provider=<public_connstr>
    Initializes node.

    setadm: give worker name with switch --worker.

Node Administration:
  pause                             Pause a consumer.
  resume                            Resume a consumer.
  wait-root                         Wait until node has catched up to root
  wait-provider                     Wait until node has catched up to provider
  change-provider --provider NEW    Change where consumer reads from

     setadm: --node and/or --consumer switches to specify
     either node or consumer.

Works, naming problems:

  status                Show set state      [set-status]
  members               Show members in set [nodes]
  takeover FROMNODE [--all] [--dead]

Broken:

  drop-node NAME
  show-consumers [--node]
  tag-dead NODE ..      Tag node as dead
  tag-alive NODE ..     Tag node as alive
"""

class CascadeAdmin(skytools.AdminScript):
    """Cascaded pgq administration."""
    queue_name = None
    queue_info = None
    extra_objs = []
    local_node = None
    root_node_name = None

    def __init__(self, svc_name, dbname, args, worker_setup = False):
        skytools.AdminScript.__init__(self, svc_name, args)
        self.initial_db_name = dbname
        if worker_setup:
            self.options.worker = self.job_name
            self.options.consumer = self.job_name

    def init_optparse(self, parser = None):
        """Add SetAdmin switches to parser."""
        p = skytools.AdminScript.init_optparse(self, parser)
        p.set_usage(command_usage.strip())

        g = optparse.OptionGroup(p, "actual queue admin options")
        g.add_option("--connstr", action="store_true",
                     help = "initial connect string")
        g.add_option("--provider",
                     help = "init: connect string for provider")
        g.add_option("--queue",
                     help = "specify queue name")
        g.add_option("--worker",
                     help = "create: specify worker name")
        g.add_option("--node",
                     help = "specify node name")
        g.add_option("--consumer",
                     help = "specify consumer name")
        g.add_option("--target",
                    help = "takeover: specify node to take over")
        g.add_option("--merge",
                    help = "create-node: combined queue name")
        g.add_option("--dead", action="append",
                    help = "tag some node as dead")
        g.add_option("--dead-root", action="store_true",
                    help = "tag some node as dead")
        g.add_option("--dead-branch", action="store_true",
                    help = "tag some node as dead")
        g.add_option("--sync-watermark",
                    help = "list of node names to sync with")
        p.add_option_group(g)
        return p

    def reload(self):
        """Reload config."""
        skytools.AdminScript.reload(self)
        if self.options.queue:
            self.queue_name = self.options.queue
        else:
            self.queue_name = self.cf.get('queue_name', '')
            if not self.queue_name:
                self.queue_name = self.cf.get('pgq_queue_name', '')
                if not self.queue_name:
                    raise Exception('"queue_name" not specified in config')

    #
    # Node initialization.
    #

    def cmd_install(self):
        db = self.get_database("db")
        self.install_code(db)

    def cmd_create_root(self, node_name, node_location):
        return self.create_node('root', node_name, node_location)

    def cmd_create_branch(self, node_name, node_location):
        return self.create_node('branch', node_name, node_location)

    def cmd_create_leaf(self, node_name, node_location):
        return self.create_node('leaf', node_name, node_location)

    def create_node(self, node_type, node_name, node_location):
        """Generic node init."""
        provider_loc = self.options.provider

        if node_type not in ('root', 'branch', 'leaf'):
            raise Exception('unknown node type')

        # connect to database
        db = self.get_database("new_node", connstr = node_location)

        # check if code is installed
        self.install_code(db)

        # query current status
        res = self.exec_query(db, "select * from pgq_node.get_node_info(%s)", [self.queue_name])
        info = res[0]
        if info['node_type'] is not None:
            self.log.info("Node is already initialized as %s" % info['node_type'])
            return

        self.log.info("Initializing node")
        node_attrs = {}

        worker_name = self.options.worker
        if not worker_name:
            raise Exception('--worker required')
        combined_queue = self.options.merge
        if combined_queue and node_type != 'leaf':
            raise Exception('--merge can be used only for leafs')

        if self.options.sync_watermark:
            if node_type != 'branch':
                raise UsageError('--sync-watermark can be used only for branch nodes')
            node_attrs['sync_watermark'] = self.options.sync_watermark

        # register member
        if node_type == 'root':
            global_watermark = None
            combined_queue = None
            provider_name = None
            self.exec_cmd(db, "select * from pgq_node.register_location(%s, %s, %s, false)",
                          [self.queue_name, node_name, node_location])
            self.exec_cmd(db, "select * from pgq_node.create_node(%s, %s, %s, %s, %s, %s, %s)",
                          [self.queue_name, node_type, node_name, worker_name, provider_name, global_watermark, combined_queue])
            provider_db = None
        else:
            if not provider_loc:
                raise Exception('Please specify --provider')

            root_db = self.find_root_db(provider_loc)
            queue_info = self.load_queue_info(root_db)

            # check if member already exists
            if queue_info.get_member(node_name) is not None:
                self.log.error("Node '%s' already exists" % node_name)
                sys.exit(1)

            combined_set = None

            provider_db = self.get_database('provider_db', connstr = provider_loc)
            q = "select node_type, node_name from pgq_node.get_node_info(%s)"
            res = self.exec_query(provider_db, q, [self.queue_name])
            row = res[0]
            if not row['node_name']:
                raise Exception("provider node not found")
            provider_name = row['node_name']

            # register member on root
            self.exec_cmd(root_db, "select * from pgq_node.register_location(%s, %s, %s, false)",
                          [self.queue_name, node_name, node_location])

            # lookup provider
            provider = queue_info.get_member(provider_name)
            if not provider:
                self.log.error("Node %s does not exist" % provider_name)
                sys.exit(1)

            # register on provider
            self.exec_cmd(provider_db, "select * from pgq_node.register_location(%s, %s, %s, false)",
                          [self.queue_name, node_name, node_location])
            rows = self.exec_cmd(provider_db, "select * from pgq_node.register_subscriber(%s, %s, %s, null)",
                                 [self.queue_name, node_name, worker_name])
            global_watermark = rows[0]['global_watermark']

            # initialize node itself

            # insert members
            self.exec_cmd(db, "select * from pgq_node.register_location(%s, %s, %s, false)",
                          [self.queue_name, node_name, node_location])
            for m in queue_info.member_map.values():
                self.exec_cmd(db, "select * from pgq_node.register_location(%s, %s, %s, %s)",
                              [self.queue_name, m.name, m.location, m.dead])

            # real init
            self.exec_cmd(db, "select * from pgq_node.create_node(%s, %s, %s, %s, %s, %s, %s)",
                          [ self.queue_name, node_type, node_name, worker_name,
                            provider_name, global_watermark, combined_queue ])


        self.extra_init(node_type, db, provider_db)

        if node_attrs:
            s_attrs = skytools.db_urlencode(node_attrs)
            self.exec_cmd(db, "select * from pgq_node.set_node_attrs(%s, %s)",
                          [self.queue_name, s_attrs])

        self.log.info("Done")

    def extra_init(self, node_type, node_db, provider_db):
        """Callback to do specific init."""
        pass

    def find_root_db(self, initial_loc = None):
        """Find root node, having start point."""
        if initial_loc:
            loc = initial_loc
        else:
            loc = self.cf.get(self.initial_db_name)

        while 1:
            db = self.get_database('root_db', connstr = loc)


            # query current status
            res = self.exec_query(db, "select * from pgq_node.get_node_info(%s)", [self.queue_name])
            info = res[0]
            node_type = info['node_type']
            if node_type is None:
                self.log.info("Root node not initialized?")
                sys.exit(1)

            self.log.debug("db='%s' -- type='%s' provider='%s'" % (loc, node_type, info['provider_location']))
            # configured db may not be root anymore, walk upwards then
            if node_type in ('root', 'combined-root'):
                db.commit()
                self.root_node_name = info['node_name']
                return db

            self.close_database('root_db')
            if loc == info['provider_location']:
                raise Exception("find_root_db: got loop: %s" % loc)
            loc = info['provider_location']
            if loc is None:
                self.log.error("Sub node provider not initialized?")
                sys.exit(1)
        raise Exception('process canceled')

    def find_root_node(self):
        self.find_root_db()
        return self.root_node_name

    def find_consumer_check(self, node, consumer):
        cmap = self.get_node_consumer_map(node)
        return (consumer in cmap)

    def find_consumer(self, node = None, consumer = None):
        if not node and not consumer:
            node = self.options.node
            consumer = self.options.consumer
        if not node and not consumer:
            raise Exception('Need either --node or --consumer')

        # specific node given
        if node:
            if consumer:
                if not self.find_consumer_check(node, consumer):
                    raise Exception('Consumer not found')
            else:
                state = self.get_node_info(node)
                consumer = state.worker_name
            return (node, consumer)

        # global consumer search
        if self.find_consumer_check(self.local_node, consumer):
            return (self.local_node, consumer)

        # fixme: dead node handling?
        nodelist = self.queue_info.member_map.keys()
        for node in nodelist:
            if node == self.local_node:
                continue
            if self.find_consumer_check(node, consumer):
                return (node, consumer)

        raise Exception('Consumer not found')

    def install_code(self, db):
        """Install cascading code to db."""
        objs = [
            skytools.DBLanguage("plpgsql"),
            #skytools.DBFunction("txid_current_snapshot", 0, sql_file="txid.sql"),
            skytools.DBSchema("pgq", sql_file="pgq.sql"),
            skytools.DBFunction("pgq.get_batch_cursor", 3, sql_file = "pgq.upgrade.2to3.sql"),
            skytools.DBSchema("pgq_ext", sql_file="pgq_ext.sql"), # not needed actually
            skytools.DBSchema("pgq_node", sql_file="pgq_node.sql"),
        ]
        objs += self.extra_objs
        skytools.db_install(db.cursor(), objs, self.log)
        db.commit()

    #
    # Print status of whole set.
    #

    def cmd_status(self):
        """Show set status."""
        self.load_local_info()

        for mname, minf in self.queue_info.member_map.iteritems():
            #inf = self.get_node_info(mname)
            #self.queue_info.add_node(inf)
            #continue

            if not self.node_alive(mname):
                node = NodeInfo(self.queue_name, None, node_name = mname)
                self.queue_info.add_node(node)
                continue
            try:
                db = self.get_database('look_db', connstr = minf.location, autocommit = 1)
                curs = db.cursor()
                curs.execute("select * from pgq_node.get_node_info(%s)", [self.queue_name])
                node = NodeInfo(self.queue_name, curs.fetchone())
                node.load_status(curs)
                self.load_extra_status(curs, node)
                self.queue_info.add_node(node)
            except DBError, d:
                msg = str(d).strip().split('\n', 1)[0]
                print('Node %s failure: %s' % (mname, msg))
                node = NodeInfo(self.queue_name, None, node_name = mname)
                self.queue_info.add_node(node)
            self.close_database('look_db')

        self.queue_info.print_tree()

    def load_extra_status(self, curs, node):
        """Fetch extra info."""
        pass

    #
    # Normal commands.
    #

    def cmd_change_provider(self):
        """Change node provider."""

        self.load_local_info()
        self.change_provider(
                node = self.options.node,
                consumer = self.options.consumer,
                new_provider = self.options.provider)

    def node_change_provider(self, node, new_provider):
        self.change_provider(node, new_provider = new_provider)

    def change_provider(self, node = None, consumer = None, new_provider = None):
        old_provider = None
        if not new_provider:
            raise Exception('Please give --provider')

        if not node or not consumer:
            node, consumer = self.find_consumer(node = node, consumer = consumer)

        cmap = self.get_node_consumer_map(node)
        cinfo = cmap[consumer]
        old_provider = cinfo['provider_node']

        if old_provider == new_provider:
            self.log.info("Consumer '%s' at node '%s' has already '%s' as provider" % (
                            consumer, node, new_provider))
            return

        # pause target node
        self.pause_consumer(node, consumer)

        # reload node info
        node_db = self.get_node_database(node)
        qinfo = self.load_queue_info(node_db)
        ninfo = qinfo.local_node
        node_location = qinfo.get_member(node).location

        # reload consumer info
        cmap = self.get_node_consumer_map(node)
        cinfo = cmap[consumer]

        # is it node worker or plain consumer?
        is_worker = (ninfo.worker_name == consumer)

        # fixme: expect the node to be described already
        q = "select * from pgq_node.register_location(%s, %s, %s, false)"
        self.node_cmd(new_provider, q, [self.queue_name, node, node_location])

        # subscribe on new provider
        if is_worker:
            q = 'select * from pgq_node.register_subscriber(%s, %s, %s, %s)'
            self.node_cmd(new_provider, q, [self.queue_name, node, consumer, cinfo['last_tick_id']])
        else:
            q = 'select * from pgq.register_consumer_at(%s, %s, %s)'
            self.node_cmd(new_provider, q, [self.queue_name, consumer, cinfo['last_tick_id']])

        # change provider on target node
        q = 'select * from pgq_node.change_consumer_provider(%s, %s, %s)'
        self.node_cmd(node, q, [self.queue_name, consumer, new_provider])

        # done
        self.resume_consumer(node, consumer)

        # unsubscribe from old provider
        try:
            if is_worker:
                q = "select * from pgq_node.unregister_subscriber(%s, %s)"
                self.node_cmd(old_provider, q, [self.queue_name, node])
            else:
                q = "select * from pgq.unregister_consumer(%s, %s)"
                self.node_cmd(old_provider, q, [self.queue_name, consumer])
        except skytools.DBError, d:
            self.log.warning("failed to unregister from old provider (%s): %s", old_provider, str(d))

    def cmd_rename_node(self, old_name, new_name):
        """Rename node."""

        self.load_local_info()

        root_db = self.find_root_db()

        # pause target node
        self.pause_node(old_name)
        node = self.load_node_info(old_name)
        provider_node = node.provider_node
        subscriber_list = self.get_node_subscriber_list(old_name)


        # create copy of member info / subscriber+queue info
        step1 = 'select * from pgq_node.rename_node_step1(%s, %s, %s)'
        # rename node itself, drop copies
        step2 = 'select * from pgq_node.rename_node_step2(%s, %s, %s)'

        # step1
        self.exec_cmd(root_db, step1, [self.queue_name, old_name, new_name])
        self.node_cmd(provider_node, step1, [self.queue_name, old_name, new_name])
        self.node_cmd(old_name, step1, [self.queue_name, old_name, new_name])
        for child in subscriber_list:
            self.node_cmd(child, step1, [self.queue_name, old_name, new_name])

        # step1
        self.node_cmd(old_name, step2, [self.queue_name, old_name, new_name])
        self.node_cmd(provider_node, step1, [self.queue_name, old_name, new_name])
        for child in subscriber_list:
            self.node_cmd(child, step2, [self.queue_name, old_name, new_name])
        self.exec_cmd(root_db, step2, [self.queue_name, old_name, new_name])

        # resume node
        self.resume_node(old_name)

    def cmd_drop_node(self, node_name):
        """Drop a node."""

        self.load_local_info()

        try:
            node = self.load_node_info(node_name)
            if node:
                # see if we can safely drop
                subscriber_list = self.get_node_subscriber_list(node_name)
                if subscriber_list:
                    raise UsageError('node still has subscribers')
        except skytools.DBError:
            pass

        try:
            # unregister node location from root node (event will be added to queue)
            root_db = self.find_root_db()
            q = "select * from pgq_node.unregister_location(%s, %s)"
            self.exec_cmd(root_db, q, [self.queue_name, node_name])
        except skytools.DBError, d:
            self.log.warning("Unregister from root failed: %s", str(d))

        try:
            # drop node info
            db = self.get_node_database(node_name)
            q = "select * from pgq_node.drop_node(%s, %s)"
            self.exec_cmd(db, q, [self.queue_name, node_name])
        except skytools.DBError, d:
            self.log.warning("Local drop failure: %s", str(d))

        # brute force removal
        for n in self.queue_info.member_map.values():
            try:
                q = "select * from pgq_node.drop_node(%s, %s)"
                self.node_cmd(n.name, q, [self.queue_name, node_name])
            except skytools.DBError, d:
                self.log.warning("Failed to remove from '%s': %s", n.name, str(d))




    def node_depends(self, sub_node, top_node):
        cur_node = sub_node
        # walk upstream
        while 1:
            info = self.get_node_info(cur_node)
            if cur_node == top_node:
                # yes, top_node is sub_node's provider
                return True
            if info.type == 'root':
                # found root, no dependancy
                return False
            # step upwards
            cur_node = info.provider_node

    def demote_node(self, oldnode, step, newnode):
        """Downgrade old root?"""
        q = "select * from pgq_node.demote_root(%s, %s, %s)"
        res = self.node_cmd(oldnode, q, [self.queue_name, step, newnode])
        if res:
            return res[0]['last_tick']

    def promote_branch(self, node):
        """Promote old branch as root."""
        q = "select * from pgq_node.promote_branch(%s)"
        self.node_cmd(node, q, [self.queue_name])

    def wait_for_catchup(self, new, last_tick):
        """Wait until new_node catches up to old_node."""
        # wait for it on subscriber
        info = self.load_node_info(new)
        if info.completed_tick >= last_tick:
            self.log.info('tick already exists')
            return info
        if info.paused:
            self.log.info('new node seems paused, resuming')
            self.resume_node(new)
        while 1:
            self.log.debug('waiting for catchup: need=%d, cur=%d' % (last_tick, info.completed_tick))
            time.sleep(1)
            info = self.load_node_info(new)
            if info.completed_tick >= last_tick:
                return info


    def takeover_root(self, old_node_name, new_node_name, failover = False):
        """Root switchover."""

        new_info = self.get_node_info(new_node_name)
        old_info = None

        if self.node_alive(old_node_name):
            old_info = self.get_node_info(old_node_name)
            self.pause_node(old_node_name)
            self.demote_node(old_node_name, 1, new_node_name)
            last_tick = self.demote_node(old_node_name, 2, new_node_name)
            self.wait_for_catchup(new_node_name, last_tick)

        self.pause_node(new_node_name)
        self.promote_branch(new_node_name)

        if self.node_alive(old_node_name):
            q = 'select * from pgq_node.register_subscriber(%s, %s, %s, %s)'
            self.node_cmd(new_node_name, q, [self.queue_name, old_node_name, old_info.worker_name, last_tick])

        q = "select * from pgq_node.unregister_subscriber(%s, %s)"
        self.node_cmd(new_info.provider_node, q, [self.queue_name, new_node_name])

        self.resume_node(new_node_name)

        if self.node_alive(old_node_name):
            self.demote_node(old_node_name, 3, new_node_name)
            self.resume_node(old_node_name)

    def takeover_nonroot(self, old_node_name, new_node_name, failover):
        """Non-root switchover."""
        if self.node_depends(new_node_name, old_node_name):
            # yes, old_node is new_nodes provider,
            # switch it around
            pnode = self.find_provider(old_node_name)
            self.node_change_provider(new_node_name, pnode)

        self.node_change_provider(old_node_name, new_node_name)

    def cmd_takeover(self, old_node_name):
        """Generic node switchover."""
        self.log.info("old: %s" % old_node_name)
        self.load_local_info()
        new_node_name = self.options.node
        if not new_node_name:
            worker = self.options.consumer
            if not worker:
                raise UsageError('old node not given')
            if self.queue_info.local_node.worker_name != worker:
                raise UsageError('old node not given')
            new_node_name = self.local_node
        if not old_node_name:
            raise UsageError('old node not given')

        if old_node_name not in self.queue_info.member_map:
            raise UsageError('Unknown node: %s' % old_node_name)

        if self.options.dead_root:
            otype = 'root'
            failover = True
        elif self.options.dead_branch:
            otype = 'branch'
            failover = True
        else:
            onode = self.get_node_info(old_node_name)
            otype = onode.type
            failover = False

        if failover:
            self.cmd_tag_dead(old_node_name)

        new_node = self.get_node_info(new_node_name)
        if old_node_name == new_node.name:
            self.log.info("same node?")
            return

        if otype == 'root':
            self.takeover_root(old_node_name, new_node_name, failover)
        else:
            self.takeover_nonroot(old_node_name, new_node_name, failover)

        # switch subscribers around
        if self.options.all or failover:
            for n in self.find_subscribers_for(old_node_name):
                self.node_change_provider(n, new_node_name)

    def find_provider(self, node_name):
        if self.node_alive(node_name):
            info = self.get_node_info(node_name)
            return info.provider_node
        nodelist = self.queue_info.member_map.keys()
        for n in nodelist:
            if n == node_name:
                continue
            if not self.node_alive(n):
                continue
            if node_name in self.get_node_subscriber_list(n):
                return n
        return self.find_root_node()

    def find_subscribers_for(self, parent_node_name):
        """Find root node, having start point."""

        # use dict to eliminate duplicates
        res = {}

        nodelist = self.queue_info.member_map.keys()
        for node_name in nodelist:
            if node_name == parent_node_name:
                continue
            if not self.node_alive(node_name):
                continue
            n = self.get_node_info(node_name)
            if not n:
                continue
            if n.provider_node == parent_node_name:
                res[n.name] = 1
        return res.keys()

    def cmd_tag_dead(self, dead_node_name):
        self.load_local_info()

        # tag node dead in memory
        self.log.info("Tagging node '%s' as dead" % dead_node_name)
        self.queue_info.tag_dead(dead_node_name)

        # tag node dead in local node
        q = "select * from pgq_node.register_location(%s, %s, null, true)"
        self.node_cmd(self.local_node, q, [self.queue_name, dead_node_name])

        # tag node dead in other nodes
        nodelist = self.queue_info.member_map.keys()
        for node_name in nodelist:
            if not self.node_alive(node_name):
                continue
            if node_name == dead_node_name:
                continue
            if node_name == self.local_node:
                continue
            try:
                q = "select * from pgq_node.register_location(%s, %s, null, true)"
                self.node_cmd(node_name, q, [self.queue_name, dead_node_name])
            except DBError, d:
                msg = str(d).strip().split('\n', 1)[0]
                print('Node %s failure: %s' % (node_name, msg))
                self.close_node_database(node_name)

    def cmd_pause(self):
        """Pause a node"""
        self.load_local_info()
        node, consumer = self.find_consumer()
        self.pause_consumer(node, consumer)

    def cmd_resume(self):
        """Resume a node from pause."""
        self.load_local_info()
        node, consumer = self.find_consumer()
        self.resume_consumer(node, consumer)

    def cmd_members(self):
        """Show member list."""
        self.load_local_info()
        db = self.get_database(self.initial_db_name)
        desc = 'Member info on %s@%s:' % (self.local_node, self.queue_name)
        q = "select node_name, dead, node_location"\
            " from pgq_node.get_queue_locations(%s) order by 1"
        self.display_table(db, desc, q, [self.queue_name])

    def cmd_node_info(self):
        self.load_local_info()

        q = self.queue_info
        n = q.local_node
        m = q.get_member(n.name)

        stlist = []
        if m.dead:
            stlist.append('DEAD')
        if n.paused:
            stlist.append("PAUSED")
        if not n.uptodate:
            stlist.append("NON-UP-TO-DATE")
        st = ', '.join(stlist)
        if not st:
            st = 'OK'
        print('Node: %s  Type: %s  Queue: %s' % (n.name, n.type, q.queue_name))
        print('Status: %s' % st)
        if n.type != 'root':
            print('Provider: %s' % n.provider_node)
        else:
            print('Provider: --')
        print('Connect strings:')
        print('  Local   : %s' % self.cf.get('db'))
        print('  Public  : %s' % m.location)
        if n.type != 'root':
            print('  Provider: %s' % n.provider_location)
        if n.combined_queue:
            print('Combined Queue: %s  (node type: %s)' % (n.combined_queue, n.combined_type))

    def cmd_wait_root(self):
        """Wait for next tick from root."""

        self.load_local_info()

        if self.queue_info.local_node.type == 'root':
            self.log.info("Current node is root, no need to wait")
            return

        self.log.info("Finding root node")
        root_node = self.find_root_node()
        self.log.info("Root is %s", root_node)

        dst_db = self.get_database('db')
        self.wait_for_node(dst_db, root_node)

    def cmd_wait_provider(self):
        """Wait for next tick from provider."""

        self.load_local_info()

        if self.queue_info.local_node.type == 'root':
            self.log.info("Current node is root, no need to wait")
            return

        dst_db = self.get_database('db')
        node = self.queue_info.local_node.provider_node
        self.log.info("Provider is %s", node)
        self.wait_for_node(dst_db, node)

    def wait_for_node(self, dst_db, node_name):
        """Core logic for waiting."""

        self.log.info("Fetching last tick for %s", node_name)
        node_info = self.load_node_info(node_name)
        tick_id = node_info.last_tick

        self.log.info("Waiting for tick > %d", tick_id)

        q = "select * from pgq_node.get_node_info(%s)"
        dst_curs = dst_db.cursor()

        while 1:
            dst_curs.execute(q, [self.queue_name])
            row = dst_curs.fetchone()
            dst_db.commit()

            if row['ret_code'] >= 300:
                self.log.warning("Problem: %s", row['ret_code'], row['ret_note'])
                return

            if row['worker_last_tick'] > tick_id:
                self.log.info("Got tick %d, exiting", row['worker_last_tick'])
                break

            self.sleep(2)

    #
    # Shortcuts for operating on nodes.
    #

    def load_local_info(self):
        """fetch set info from local node."""
        db = self.get_database(self.initial_db_name)
        self.queue_info = self.load_queue_info(db)
        self.local_node = self.queue_info.local_node.name

    def get_node_database(self, node_name):
        """Connect to node."""
        if node_name == self.queue_info.local_node.name:
            db = self.get_database(self.initial_db_name)
        else:
            m = self.queue_info.get_member(node_name)
            if not m:
                self.log.error("get_node_database: cannot resolve %s" % node_name)
                sys.exit(1)
            #self.log.info("%s: dead=%s" % (m.name, m.dead))
            if m.dead:
                return None
            loc = m.location
            db = self.get_database('node.' + node_name, connstr = loc)
        return db

    def node_alive(self, node_name):
        m = self.queue_info.get_member(node_name)
        if not m:
            res = False
        elif m.dead:
            res = False
        else:
            res = True
        #self.log.warning('node_alive(%s) = %s' % (node_name, res))
        return res

    def close_node_database(self, node_name):
        """Disconnect node's connection."""
        if node_name == self.queue_info.local_node.name:
            self.close_database(self.initial_db_name)
        else:
            self.close_database("node." + node_name)

    def node_cmd(self, node_name, sql, args, quiet = False):
        """Execute SQL command on particular node."""
        db = self.get_node_database(node_name)
        if not db:
            self.log.warning("ignoring cmd for dead node '%s': %s" % (
                node_name, skytools.quote_statement(sql, args)))
            return None
        return self.exec_cmd(db, sql, args, quiet = quiet, prefix=node_name)

    #
    # Various operation on nodes.
    #

    def set_paused(self, node, consumer, pause_flag):
        """Set node pause flag and wait for confirmation."""

        q = "select * from pgq_node.set_consumer_paused(%s, %s, %s)"
        self.node_cmd(node, q, [self.queue_name, consumer, pause_flag])

        self.log.info('Waiting for worker to accept')
        while 1:
            q = "select * from pgq_node.get_consumer_state(%s, %s)"
            stat = self.node_cmd(node, q, [self.queue_name, consumer], quiet = 1)[0]
            if stat['paused'] != pause_flag:
                raise Exception('operation canceled? %s <> %s' % (repr(stat['paused']), repr(pause_flag)))

            if stat['uptodate']:
                op = pause_flag and "paused" or "resumed"
                self.log.info("Consumer '%s' on node '%s' %s" % (consumer, node, op))
                return
            time.sleep(1)
        raise Exception('process canceled')


    def pause_consumer(self, node, consumer):
        """Shortcut for pausing by name."""
        self.set_paused(node, consumer, True)

    def resume_consumer(self, node, consumer):
        """Shortcut for resuming by name."""
        self.set_paused(node, consumer, False)

    def pause_node(self, node):
        """Shortcut for pausing by name."""
        state = self.get_node_info(node)
        self.pause_consumer(node, state.worker_name)

    def resume_node(self, node):
        """Shortcut for resuming by name."""
        state = self.get_node_info(node)
        if state:
            self.resume_consumer(node, state.worker_name)

    def subscribe_node(self, target_node, subscriber_node, tick_pos):
        """Subscribing one node to another."""
        q = "select * from pgq_node.subscribe_node(%s, %s, %s)"
        self.node_cmd(target_node, q, [self.queue_name, subscriber_node, tick_pos])

    def unsubscribe_node(self, target_node, subscriber_node):
        """Unsubscribing one node from another."""
        q = "select * from pgq_node.unsubscribe_node(%s, %s)"
        self.node_cmd(target_node, q, [self.queue_name, subscriber_node])

    _node_cache = {}
    def get_node_info(self, node_name):
        """Cached node info lookup."""
        if node_name in self._node_cache:
            return self._node_cache[node_name]
        inf = self.load_node_info(node_name)
        self._node_cache[node_name] = inf
        return inf

    def load_node_info(self, node_name):
        """Non-cached node info lookup."""
        db = self.get_node_database(node_name)
        if not db:
            self.log.warning('load_node_info(%s): ignoring dead node' % node_name)
            return None
        q = "select * from pgq_node.get_node_info(%s)"
        rows = self.exec_query(db, q, [self.queue_name])
        return NodeInfo(self.queue_name, rows[0])

    def load_queue_info(self, db):
        """Non-cached set info lookup."""
        res = self.exec_query(db, "select * from pgq_node.get_node_info(%s)", [self.queue_name])
        info = res[0]

        q = "select * from pgq_node.get_queue_locations(%s)"
        member_list = self.exec_query(db, q, [self.queue_name])

        qinf = QueueInfo(self.queue_name, info, member_list)
        if self.options.dead:
            for node in self.options.dead:
                self.log.info("Assuming node '%s' as dead" % node)
                qinf.tag_dead(node)
        return qinf

    def get_node_subscriber_list(self, node_name):
        """Fetch subscriber list from a node."""
        q = "select node_name, node_watermark from pgq_node.get_subscriber_info(%s)"
        db = self.get_node_database(node_name)
        rows = self.exec_query(db, q, [self.queue_name])
        return [r['node_name'] for r in rows]

    def get_node_consumer_map(self, node_name):
        """Fetch consumer list from a node."""
        q = "select consumer_name, provider_node, last_tick_id from pgq_node.get_consumer_info(%s)"
        db = self.get_node_database(node_name)
        rows = self.exec_query(db, q, [self.queue_name])
        res = {}
        for r in rows:
            res[r['consumer_name']] = r
        return res

if __name__ == '__main__':
    script = CascadeAdmin('setadm', 'node_db', sys.argv[1:], worker_setup = False)
    script.start()

