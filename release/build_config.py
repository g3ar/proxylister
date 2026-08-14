"""Site-specific connection configuration for the ProxyLister PVE build lab.

Change ``PVE_HOST`` when the dedicated build-lab server moves. The build
control plane always connects to this host as ``root``; credentials remain
outside the repository and are selected by ``release.buildlib.pve``.
"""

PVE_HOST = "192.168.66.2"
