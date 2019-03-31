import six

from oslo_log import log as logging

from hpedockerplugin.cmd import cmd
from hpedockerplugin.cmd.cmd_claimavailableip import ClaimAvailableIPCmd
from hpedockerplugin.cmd.cmd_createfpg import CreateFpgCmd
from hpedockerplugin.cmd.cmd_createvfs import CreateVfsCmd

from hpedockerplugin import exception
from hpedockerplugin.hpe import share

LOG = logging.getLogger(__name__)


class CreateShareCmd(cmd.Cmd):
    def __init__(self, file_mgr, share_args):
        self._file_mgr = file_mgr
        self._etcd = file_mgr.get_etcd()
        self._fp_etcd = file_mgr.get_file_etcd()
        self._mediator = file_mgr.get_mediator()
        self._config = file_mgr.get_config()
        self._backend = file_mgr.get_backend()
        self._share_args = share_args
        # self._size = share_args['size']
        self._cmds = []

        # Initialize share state
        self._etcd.save_share({
            'name': share_args['name'],
            'backend': self._backend,
            'status': 'CREATING'
        })

    def unexecute(self):
        self._etcd.delete_share(self._share_args)
        for command in reversed(self._cmds):
            command.unexecute()

    def _create_share(self):
        share_etcd = self._file_mgr.get_etcd()
        try:
            share_id = self._mediator.create_share(self._share_args)
            self._share_args['id'] = share_id
        except Exception as ex:
            msg = "Share creation failed [share_name: %s, error: %s" %\
                  (self._share_args['name'], six.text_type(ex))
            LOG.error(msg)
            self.unexecute()
            raise exception.ShareCreationFailed(msg)

        try:
            self._share_args['status'] = 'AVAILABLE'
            share_etcd.save_share(self._share_args)
            self._increment_share_cnt_for_fpg()
        except Exception as ex:
            msg = "Share creation failed [share_name: %s, error: %s" %\
                  (self._share_args['name'], six.text_type(ex))
            LOG.error(msg)
            # TODO:
            self._mediator.delete_share(self._share_args)
            self.unexecute()
            raise exception.ShareCreationFailed(msg)

    # FPG lock is already acquired in this flow
    def _increment_share_cnt_for_fpg(self):
        cpg_name = self._share_args['cpg']
        fpg_name = self._share_args['fpg']
        fpg = self._fp_etcd.get_fpg_metadata(self._backend, cpg_name,
                                             fpg_name)
        cnt = fpg.get('share_cnt', 0) + 1
        fpg['share_cnt'] = cnt
        if cnt >= share.MAX_SHARES_PER_FPG:
            fpg['reached_full_capacity'] = True
        self._fp_etcd.save_fpg_metadata(self._backend, cpg_name,
                                        fpg_name, fpg)


class CreateShareOnNewFpgCmd(CreateShareCmd):
    def __init__(self, file_mgr, share_args, make_default_fpg=False):
        super(CreateShareOnNewFpgCmd, self).__init__(file_mgr, share_args)
        self._make_default_fpg = make_default_fpg

    def execute(self):
        return self._create_share_on_new_fpg()

    def _create_share_on_new_fpg(self):
        cpg_name = self._share_args['cpg']
        fpg_name = self._share_args['fpg']
        vfs_name = self._share_args['vfs']
        try:
            create_fpg_cmd = CreateFpgCmd(self._file_mgr, cpg_name,
                                          fpg_name, self._make_default_fpg)
            create_fpg_cmd.execute()
            self._cmds.append(create_fpg_cmd)
        except exception.FpgCreationFailed as ex:
            msg = "Create share on new FPG failed. Msg: %s" \
                  % six.text_type(ex)
            LOG.error(msg)
            raise exception.ShareCreationFailed(reason=msg)

        config = self._file_mgr.get_config()
        claim_free_ip_cmd = ClaimAvailableIPCmd(self._backend,
                                                config,
                                                self._fp_etcd)
        try:
            ip, netmask = claim_free_ip_cmd.execute()
            self._cmds.append(claim_free_ip_cmd)

            create_vfs_cmd = CreateVfsCmd(self._file_mgr, cpg_name,
                                          fpg_name, vfs_name, ip, netmask)
            create_vfs_cmd.execute()
            self._cmds.append(create_vfs_cmd)

            # Now that VFS has been created successfully, move the IP from
            # locked-ip-list to ips-in-use list
            claim_free_ip_cmd.mark_ip_in_use()
            self._share_args['vfsIPs'] = [(ip, netmask)]

        except exception.IPAddressPoolExhausted as ex:
            msg = "Create VFS failed. Msg: %s" % six.text_type(ex)
            LOG.error(msg)
            raise exception.VfsCreationFailed(reason=msg)
        except exception.VfsCreationFailed as ex:
            msg = "Create share on new FPG failed. Msg: %s" \
                  % six.text_type(ex)
            LOG.error(msg)
            self.unexecute()
            raise exception.ShareCreationFailed(reason=msg)

        self._share_args['fpg'] = fpg_name
        self._share_args['vfs'] = vfs_name

        # All set to create share at this point
        return self._create_share()


class CreateShareOnDefaultFpgCmd(CreateShareCmd):
    def __init__(self, file_mgr, share_args):
        super(CreateShareOnDefaultFpgCmd, self).__init__(file_mgr, share_args)

    def execute(self):
        try:
            fpg_info = self._get_default_available_fpg()
            fpg_name = fpg_info['fpg']
            with self._fp_etcd.get_fpg_lock(self._backend, fpg_name):
                self._share_args['fpg'] = fpg_name
                self._share_args['vfs'] = fpg_info['vfs']
                # Only one IP per FPG is supported at the moment
                # Given that, list can be dropped
                subnet_ips_map = fpg_info['ips']
                subnet, ips = next(iter(subnet_ips_map.items()))
                self._share_args['vfsIPs'] = [(ips[0], subnet)]
                return self._create_share()
        except Exception as ex:
            # It may be that a share on some full FPG was deleted by
            # the user and as a result leaving an empty slot. Check
            # all the FPGs that were created as default and see if
            # any of those have share count less than MAX_SHARE_PER_FPG
            try:
                all_fpgs_for_cpg = self._fp_etcd.get_all_fpg_metadata(
                    self._backend, self._share_args['cpg']
                )
                for fpg in all_fpgs_for_cpg:
                    fpg_name = fpg['fpg']
                    if fpg_name.startswith("Docker"):
                        with self._fp_etcd.get_fpg_lock(self._backend,
                                                        fpg_name):
                            if fpg['share_cnt'] < share.MAX_SHARES_PER_FPG:
                                self._share_args['fpg'] = fpg_name
                                self._share_args['vfs'] = fpg['vfs']
                                # Only one IP per FPG is supported
                                # Given that, list can be dropped
                                subnet_ips_map = fpg['ips']
                                items = subnet_ips_map.items()
                                subnet, ips = next(iter(items))
                                self._share_args['vfsIPs'] = [(ips[0],
                                                               subnet)]
                                return self._create_share()
            except Exception:
                pass
            raise ex

    # If default FPG is full, it raises exception
    # EtcdMaxSharesPerFpgLimitException
    def _get_default_available_fpg(self):
        fpg_name = self._get_current_default_fpg_name()
        fpg_info = self._fp_etcd.get_fpg_metadata(self._backend,
                                                  self._share_args['cpg'],
                                                  fpg_name)
        if fpg_info['share_cnt'] >= share.MAX_SHARES_PER_FPG:
            raise exception.EtcdMaxSharesPerFpgLimitException(
                fpg_name=fpg_name)
        return fpg_info

    def _get_current_default_fpg_name(self):
        cpg_name = self._share_args['cpg']
        try:
            backend_metadata = self._fp_etcd.get_backend_metadata(
                self._backend)
            return backend_metadata['default_fpgs'].get(cpg_name)
        except exception.EtcdMetadataNotFound:
            raise exception.EtcdDefaultFpgNotPresent(cpg=cpg_name)


class CreateShareOnExistingFpgCmd(CreateShareCmd):
    def __init__(self, file_mgr, share_args):
        super(CreateShareOnExistingFpgCmd, self).__init__(file_mgr,
                                                          share_args)

    def execute(self):
        fpg_name = self._share_args['fpg']
        with self._fp_etcd.get_fpg_lock(self._backend, fpg_name):
            try:
                # Specified FPG may or may not exist. In case it
                # doesn't, EtcdFpgMetadataNotFound exception is raised
                fpg_info = self._fp_etcd.get_fpg_metadata(
                    self._backend, self._share_args['cpg'], fpg_name)
                self._share_args['vfs'] = fpg_info['vfs']
                # Only one IP per FPG is supported at the moment
                # Given that, list can be dropped
                subnet_ips_map = fpg_info['ips']
                subnet, ips = next(iter(subnet_ips_map.items()))
                self._share_args['vfsIPs'] = [(ips[0], subnet)]
                self._create_share()
            except exception.EtcdMetadataNotFound as ex:
                # Assume it's a legacy FPG, try to get details
                fpg_info = self._get_legacy_fpg()

                # CPG passed can be different than actual CPG
                # used for creating legacy FPG. Override default
                # or supplied CPG
                self._share_args['cpg'] = fpg_info['cpg']

                vfs_info = self._get_backend_vfs_for_fpg()
                vfs_name = vfs_info['name']
                ip_info = vfs_info['IPInfo'][0]

                fpg_metadata = {
                    'fpg': fpg_name,
                    'fpg_size': fpg_info['capacityGiB'],
                    'vfs': vfs_name,
                    'ips': {ip_info['netmask']: [ip_info['IPAddr']]},
                    'reached_full_capacity': False
                }
                LOG.info("Creating FPG entry in ETCD for legacy FPG: "
                         "%s" % six.text_type(fpg_metadata))

                # TODO: Consider NOT maintaing FPG information in
                # ETCD. This will always make it invoke above legacy flow
                # Create FPG entry in ETCD
                self._fp_etcd.save_fpg_metadata(self._backend,
                                                fpg_info['cpg'],
                                                fpg_name,
                                                fpg_metadata)
                self._share_args['vfs'] = vfs_name
                # Only one IP per FPG is supported at the moment
                # Given that, list can be dropped
                subnet_ips_map = fpg_metadata['ips']
                subnet, ips = next(iter(subnet_ips_map.items()))
                self._share_args['vfsIPs'] = [(ips[0], subnet)]
                self._create_share()

    def _get_legacy_fpg(self):
        return self._mediator.get_fpg(self._share_args['fpg'])

    def _get_backend_vfs_for_fpg(self):
        return self._mediator.get_vfs(self._share_args['fpg'])
