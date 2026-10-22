# 2026-10-01 10:00:00 by RouterOS 7.15.3
# software id = ABCD-1234
#
/interface bridge
add name=bridge1 vlan-filtering=yes
/interface ethernet
set [ find default-name=ether1 ] comment=Internet
set [ find default-name=ether6 ] disabled=yes
/interface vlan
add interface=bridge1 name=USERS vlan-id=10
/interface bonding
add mode=802.3ad name=bond1 slaves=ether7,ether8
/ip pool
add name=pool10 ranges=10.40.0.100-10.40.0.199
/interface bridge port
add bridge=bridge1 interface=ether2 pvid=10
add bridge=bridge1 interface=bond1
/interface bridge vlan
add bridge=bridge1 tagged=bridge1,bond1 untagged=ether2 vlan-ids=10
/ip address
add address=192.0.2.20/24 interface=ether1 network=192.0.2.0
add address=10.40.0.1/24 interface=USERS network=10.40.0.0
/ip dhcp-server
add address-pool=pool10 interface=USERS name=dhcp10
/ip dhcp-server lease
add address=10.40.0.20 mac-address=AA:BB:CC:00:11:33 comment="Printer"
/ip dhcp-server network
add address=10.40.0.0/24 dns-server=10.40.0.1 domain=office.lan gateway=\
    10.40.0.1
/ip dns
set servers=1.1.1.1,9.9.9.9
/ip route
add dst-address=0.0.0.0/0 gateway=192.0.2.1 distance=1
/routing bgp connection
add as=65040 local.role=ebgp name=isp remote.address=192.0.2.1/32 remote.as=65000
/system identity
set name=RB-01
/user
add name=backup group=read password=secret123
