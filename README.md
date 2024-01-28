# SFTelnetProxyMuxer
A telnet proxy muxer. Allows (hopefully) multiple clients to be proxied into a single remote telnet session to a remote server. 
Primary use case is a qemu process with a serial port attached to a telnet server. This allows multple connections to the serial port with all clients being able to send and receive data to telnet qemu serial port.

