"""
Copyright (C) 2017 DataCentred Ltd - All Rights Reserved
"""

import socket
import sys
import time

# 3rd party imports
import crypt
import paramiko

# Local imports
from staging_bootstrap import hypervisor_client
from staging_bootstrap import preseed_server_client
from staging_bootstrap.formatter import info
from staging_bootstrap.formatter import detail
from staging_bootstrap.puppet_config_manager import PuppetConfigManager
from staging_bootstrap.subnet_manager import SubnetManager
from staging_bootstrap.util import wait_for


class Host(object):
    """Container for a host object"""

    # Class varaible to hold default parameters
    defaults = {}

    # Class variable to hold default facts
    facts = {}

    # Class variable to hold default class excludes
    excludes = {}

    def __init__(self, name, value):
        self.name = name
        self.config = value


    def get_config(self, name):
        """Gets host configuration prefering instance parameters over class"""
        if name in self.config:
            return self.config[name]
        return self.defaults[name]


    def get_network_config(self, name, index=0):
        """Gets network config prefering instance parameters over subnet"""
        network = self.config['networks'][index]
        if name in network:
            return network[name]

        subnet = SubnetManager.get(network['subnet'])
        try:
            return getattr(subnet, name)
        except AttributeError:
            return None

    @property
    def role(self):
        return self.get_config('role')

    @property
    def domain(self):
        return self.name[self.name.index('.')+1:]

    @property
    def memory(self):
        return self.get_config('memory')

    @property
    def disks(self):
        return self.get_config('disks')

    @property
    def password(self):
        return self.get_config('password')

    @property
    def location(self):
        return self.get_config('location')

    @property
    def address(self):
        return self.get_network_config('address')

    @property
    def netmask(self):
        return self.get_network_config('netmask')

    @property
    def gateway(self):
        return self.get_network_config('gateway')

    @property
    def nameservers(self):
        return self.get_network_config('nameservers')

    def exists(self):
        """Check if a host exists"""
        hypervisor = hypervisor_client.HypervisorClient('localhost')
        try:
            hypervisor.host_get(self.name)
        except RuntimeError:
            return False
        return True


    def create_networks(self, hypervisor):
        """Creates networks on the hypervisor and returns metadata"""

        names = []
        metadata = []
        for index, network in enumerate(self.config['networks']):
            subnet = SubnetManager.get(network['subnet'])

            # Extract static network configuration from host network and subnet objects
            metadata.append({
                'interface': network['interface'],
                'address': network['address'],
                'netmask': subnet.netmask,
                'gateway': self.get_network_config('gateway', index),
                'nameservers': self.get_network_config('nameservers', index),
                'options': self.get_network_config('options', index),
            })

            # Calculate the network name and append to the list for creation
            vlan = SubnetManager.get(network['subnet']).vlan
            name = 'vlan:{}'.format(vlan)
            detail(name)
            names.append(name)

            # Create the network if it doesn't exist
            hypervisor.network_create(name, 'br0', vlan)

        return names, metadata


    def create(self):
        """Create a host, blocking until it is provisioned and SSH is running"""

        info('Creating host {} ...'.format(self.name))
        detail('Memory  {} MB'.format(self.memory))
        detail('Disks   {} GB'.format(self.disks))
        detail('Address {}'.format(self.address))
        detail('Netmask {}'.format(self.netmask))
        detail('Gateway {}'.format(self.gateway))
        detail('DNS     {}'.format(self.nameservers))

        preseed = preseed_server_client.PreseedServerClient('localhost')
        hypervisor = hypervisor_client.HypervisorClient('localhost')

        # Create networks on the hypervisor
        info('Creating networks ...')
        network_names, network_metadata = self.create_networks(hypervisor)

        # Create a preseed entry
        metadata = {
            'root_password': crypt.crypt(self.password, '$6$salt'),
            'finish_url': 'http://{}:8421/hosts/{}/finish'.format(self.gateway, self.name),
            'networks': network_metadata,
        }
        preseed.host_create(self.name, 'preseed.xenial.erb', 'finish.xenial.erb', metadata)

        # Set the preseed kernel command line parameters, mostly static network options
        extra_args = [
            'auto=true',
            'priority=critical',
            'vga=normal',
            'hostname={}'.format(self.name),
            'domain={}'.format(self.domain),
            'url=http://{}:8421/hosts/{}/preseed'.format(self.gateway, self.name),
            'netcfg/choose_interface=auto',
            'netcfg/disable_autoconfig=true',
            'netcfg/get_ipaddress={}'.format(self.address),
            'netcfg/get_netmask={}'.format(self.netmask),
            'netcfg/get_gateway={}'.format(self.gateway),
            'netcfg/get_nameservers="{}"'.format(' '.join(self.nameservers)),
            'netcfg/confirm_static=true'
        ]

        # Create the host on the hypervisor
        # This is a non-blocking operation, so we need to poll for completion
        info('Creating host ...')
        hypervisor.host_create(self.name, self.memory, self.disks, network_names,
                               self.location, ' '.join(extra_args))
        delta = self.wait_for_state(hypervisor, 'shut off')
        detail('Host created in {}s'.format(delta))

        info('Rebooting host ...')
        hypervisor.host_start(self.name)

        # Wait for SSH to become available for provisioning
        info('Waiting for SSH ...')
        delta = self.wait_for_port(22)
        detail('Port listening in {}s'.format(delta))

        # Clean up to the preseed server
        preseed.host_delete(self.name)


    def wait_for_state(self, hypervisor, state):
        """Wait for a host to enter a specific state"""
        def cond():
            """Closure to poll host state"""
            return hypervisor.host_get(self.name)['state'] == state
        return wait_for(cond)


    def wait_for_port(self, port):
        """Wait for a port to start listening"""
        def cond():
            """Closure to socket state"""
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.connect((self.address, port))
            except IOError:
                return False
            return True
        return wait_for(cond)


    def ssh(self, command, **kwargs):
        """SSH onto a host and execute a command"""

        if 'acceptable_exitcodes' in kwargs:
            acceptable_exitcodes = kwargs['acceptable_exitcodes']
        else:
            acceptable_exitcodes = [0]

        info('Executing on {}: {}'.format(self.name, command))
        client = paramiko.client.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.address, username='root', password=self.password)
        channel = client.get_transport().open_session()
        channel.set_combine_stderr(True)
        channel.exec_command(command)
        while not channel.exit_status_ready():
            while channel.recv_ready():
                sys.stdout.write(channel.recv(8192))
            time.sleep(0.1)
        while channel.recv_ready():
            sys.stdout.write(channel.recv(8192))
        detail("Exited with status {}".format(channel.recv_exit_status()))
        if channel.recv_exit_status() not in acceptable_exitcodes:
            raise RuntimeError('command execution failed')


    def scp(self, source, target):
        """SCP a local file to a host"""

        info('Copying {} to {} on {}'.format(source, target, self.name))
        client = paramiko.client.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.address, username='root', password=self.password)
        sftp = client.open_sftp()
        sftp.put(source, target)


    def install_puppet(self):
        """Install puppet on the host"""

        deb = 'puppetlabs-release-pc1-xenial.deb'
        self.ssh(r'wget -O /tmp/{0} https://apt.puppet.com/{0}'.format(deb))
        self.ssh(r'dpkg -i --force-all /tmp/{0}'.format(deb))
        self.ssh(r'apt-get update')
        self.ssh(r'echo START=no > /etc/default/puppet')
        self.ssh(r'apt-get -y -o DPkg::Options::=--force-confold install puppet-agent')
        self.puppet_disable()
        # Temporary hack for Icinga 2 (prevents restarts and box death!)
        self.ssh(r'mkdir -p /var/lib/puppet')
        self.ssh(r'ln -s /etc/puppetlabs/puppet/ssl /var/lib/puppet')


    def configure_puppet(self):
        """Configure puppet"""
        config = PuppetConfigManager.get(self.name)
        if config is None:
            config = PuppetConfigManager.get(self.role)
        if config is None:
            config = PuppetConfigManager.get('default')
        if config is None:
            raise RuntimeError
        self.ssh('echo \'{}\' > /etc/puppetlabs/puppet/puppet.conf'.format(config.config))


    def install_puppet_modules(self, modules):
        """Install puppet modules"""

        if isinstance(modules, str):
            modules = modules.split()
        for module in modules:
            self.ssh(r'/opt/puppetlabs/bin/puppet module install {}'.format(module))


    def puppet_apply(self, manifest):
        """Transfer and apply a manifest"""

        target = '/tmp/manifest.pp'
        self.scp(manifest, target)
        command = ''
        if self.facts:
            command = command + ' '.join(map(lambda x: 'FACTER_{}={}'.format(x, self.facts[x]), self.facts)) + ' '
        command = command + '/opt/puppetlabs/bin/puppet apply ' + target
        self.puppet_enable()
        self.ssh(command)
        self.puppet_disable()


    def puppet_agent(self):
        """Run puppet against the master specifying role and excluded classes"""

        command = 'FACTER_role=' + self.get_config('role') + ' '
        if self.facts:
            command = command + ' '.join(map(lambda x: 'FACTER_{}={}'.format(x, self.facts[x]), self.facts)) + ' '
        if self.excludes:
            command = command + 'FACTER_excludes=' + ','.join(self.excludes) + ' '
        command = command + '/opt/puppetlabs/bin/puppet agent --test'
        self.puppet_enable()
        self.ssh(command, acceptable_exitcodes=[0, 2])
        self.puppet_disable()

    def puppet_enable(self):
        """Enable puppet runs"""

        self.ssh('/opt/puppetlabs/bin/puppet agent --enable')


    def puppet_disable(self):
        """Disable automatic puppet runs"""

        self.ssh('/opt/puppetlabs/bin/puppet agent --disable')


# vi: ts=4 et:
