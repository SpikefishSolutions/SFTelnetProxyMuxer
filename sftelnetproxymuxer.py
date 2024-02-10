import socket
import asyncio
import telnetlib3
import logging
import pdb
log = logging.getLogger(__name__)

class SFTelnetProxyMuxer:
    def __init__(self, remote_server=None, remote_port=None, listen_ip=None, listen_port=None, remote_reader=None, remote_writer=None, heartbeattimer=None):

        if not listen_port:
            raise ValueError("Error: listen_port is required")
        self.server = None
        # remote_server/remote_port are remote telnet server to connect to.
        # remote_reader / remote_writer are input and output channels to use instead of telneting to remote
        if (remote_server or remote_port) and (remote_reader or remote_writer):
            raise ValueError("Error: argument remote_server or remote_port can't be used along with reader or writer. Only use remote_server nad remote_port or reader / writer")

        # controls where we get data from
        self.servertype = self.find_server_type(remote_server, remote_port, remote_reader, remote_writer)

        # make the remote_info look like the same format as client_info later from sock('peername')
        self.remote_info = f"('{self.remote_server}', {self.remote_port})"
        if listen_ip == None:
            listen_ip = '0.0.0.0'
        self.listen_ip = listen_ip
        self.listen_port = listen_port

        self.clients = set()
        self.remote_reader = None
        self.remote_writer = None
        self.lock = asyncio.Lock()  # Lock for coordinating access to the remote server

        # Telnet protocol constants
        self.IAC = b"\xff" # Interpret as Command
        # Telnet NOP command. Will be used as a heartbeat to clients.
        self.NOP = b"\xf1"
        # Telnet Are You There.
        self.AYT = b"\xf6"

        # how often do we check the remote telnet server is up and each telnet client connected to gns3 is up.
        self.heartbeattimer = heartbeattimer
        if not heartbeattimer:
            self.heartbeattimer = 30
        self.closing = False

    def find_server_type(self, remote_server, remote_port, remote_reader, remote_writer):
        if remote_server and remote_port:
            self.remote_server = remote_server
            self.remote_port = remote_port
            self.server = "telnet"
        elif remote_reader and remote_writer:
            self.remote_reader = remote_reader
            self.remote_writer = remote_writer
            self.server = "readerwriter"
        else:
            raise ValueError("You must define remote_server and remote_port or reader and writer. These input are where the proxy will get data from")
         
    async def handle_client(self, reader, writer):
        client_info = writer.get_extra_info('peername')
        sock = writer.get_extra_info('socket') 
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log.debug(f"New client connected: {client_info}")
        self.clients.add(writer)

        try:
            await asyncio.sleep(1)
            while True and not self.closing:
                try:
                    # Set a timeout for the read operation, without should() the socket closes after timeout.
                    data = await asyncio.shield(asyncio.wait_for(reader.read(1024), timeout=self.heartbeattimer))
                    if reader.at_eof():
                        # should see if there was data and send it before we shutdown.
                        log.debug(f"Client {client_info} closed tcp session with eof.")
                        writer.close()
                        self.clients.discard(writer)
                        break

                    if not data:
                        log.debug(f"No data from socket read, start over read loop.")
                        continue 

                    async with self.lock:
                        log.debug(f"We have data from {self.remote_info} data: :{data}:")
                        if self.remote_writer is not None:
                            log.debug(f"Sending data from from client {client_info} to server {self.remote_info}")
                            try: 
                                self.remote_writer.write(data)
                                await asyncio.wait_for(self.remote_writer.drain(), timeout=10)
                            except asyncio.TimeoutError:
                                log.debug(f"Timeout failure waiting for write and/or drain. Closing server")
                                await self.shutdown()
                                return
                            except Exception as e:
                                log.debug(f"Unknown failure waiting for write and/or drain. Closing server: Error {e}")
                                await self.shutdown()
                                return
                           
                # ok keepalives using NOP work but we just won't get data back as the receiver of the NOP will only ack the packet.
                # This is fine as this will be enouogh for linux to start thinking about shuting down a socket that isn't
                # responding. Hard to say long that will be but ubuntu with no magic is picking 300 seconds.
                # if a flood of console traffic shows up the socket will close much sooner.
                except asyncio.TimeoutError:
                    log.debug(f"No data read from {client_info}, send heartbeat to test client socket.")
                    try:
                        log.debug(f"Heartbeat: Are you there {client_info}?")
                        writer.send_iac(self.IAC + self.NOP)
                        await asyncio.wait_for(writer.drain(), timeout=10)
                    except asyncio.TimeoutError:
                        log.debug(f"Heartbeat: Unable to send heartbeat to {client_info}, closing client socket.")
                        writer.close()
                        self.clients.discard(writer)
                        break 
                    except Exception as e:
                        log.debug(f"Heartbeat: Unknown error from {client_info}, closing client socket. Exeption {e}")
                        writer.close()
                        self.clients.discard(writer)
                        break 

                except Exception as e:
                    log.debug(f"Error in handling data from client {client_info}:")
                    writer.close()
                    self.clients.discard(writer)
                    break

        except Exception as e:
            log.debug(f"Error in managing client {client_info}: {e}")

        finally:
            # Safely remove the writer from clients set and close the connection
            writer.close()
            self.clients.discard(writer)
            log.debug(f"Client {client_info} disconnected. Remaining clients: {len(list(self.clients))}")
            log.debug(f"Connection with client {client_info} closed.")


    async def broadcast_to_clients(self, data):
        if not self.clients:
            log.debug(f"Warning: No clients connected, ignoring data.")
            return 
            
        for writer in set(self.clients):
            client_info = writer.get_extra_info('peername')
            try:
                #log.debug(f"Clients connected: {writer}, sending data: {data}")
                writer.write(data)
                await asyncio.wait_for(writer.drain(), timeout=2.0)
            except Exception as e:
                log.debug(f"Lost connection to client {client_info}")
                writer.close()
                self.clients.discard(writer)

    async def handle_remote_server(self):
        log.debug("Start handler for remote server")
        while True and not self.closing:
            log.debug("main run loop")
            try:
                if self.remote_server and self.remote_port:
                    log.debug(f"Looks like we're running a server {self.listen_ip} on {self.listen_port}.")
                    self.remote_reader, self.remote_writer = await telnetlib3.open_connection(
                        host=self.remote_server, port=self.remote_port
                    )
                    sock = self.remote_writer.get_extra_info('socket') 
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                elif self.remote_reader and self.remote_writer and not self.remote_server and not self.remote_port:
                    log.debug(f"Looks like we're running with reader and writer.")
                    if self.remote_reader.at_eof() or self.remote_writer.at_eof():
                        log.debug(f"reader/writer EOFed.")
                        break
                else:
                    raise ValueError("Server state incorrect. self.remote_reader or self.remote_writer close (eof).")
                
                while True and not self.closing:
                    try:
                        data = await asyncio.shield(asyncio.wait_for(self.remote_reader.read(1024), timeout=self.heartbeattimer))
                        if self.remote_reader.at_eof():
                            # Not %100 sure its possible to get data and have the reader socket close, but just in case.
                            if data:
                                await self.broadcast_to_clients(data)
                            log.debug(f"Remote server {self.remote_info} closed tcp session with eof.")
                            break

                        if not data:
                            log.debug(f"No data from remote telnet server {self.remote_info}.")
                            continue

                    except asyncio.TimeoutError:
                        log.debug(f"No data from server {self.remote_info}, send heartbeat to test server socket.")
                        try:
                            log.debug(f"Heartbeat: Are you there {self.remote_info}?")
                            # NOP and AYT cause QEMU to spam everyone's console with junk. 
                            # This causes everyone to close the session and eof tcp which makes me sad.
                            # Will need to research more... or did i call this wrong and just fix it?
                            self.remote_writer.send_iac(self.IAC + self.NOP)
                            await asyncio.wait_for(self.remote_writer.drain(), timeout=10)
                            continue
 
                        except asyncio.TimeoutError:
                            log.debug(f"Heartbeat: Unable to send heartbeat to {self.remote_info}, closing server socket.")
                            await self.shutdown()
                            break

                        except Exception as e:
                            log.debug(f"Heartbeat: Unknown error from {self.remote_info}, shutting down. Exeption {e}")
                            await self.shutdown()
                            break

                    except Exception as e:
                        log.debug("Failed to read socket data from server {self.remote_info} exception: {e}")
                        break
                    # This can generate a lot of log noise.
                    #if not self.clients:
                    #    log.debug("No clients connected, but console data found. Skipping.")
                    #    continue
                    #log.debug(f"Sending data to clients data: {data}")
                    await self.broadcast_to_clients(data)

            except ConnectionRefusedError as e:
                error_msg = f"Warning: Connection to remote server {self.remote_info} refused."
                log.debug(error_msg)
                await self.broadcast_to_clients(f"\r{error_msg}\n\r")

            except TimeoutError as e:
                error_msg = f"Warning: Connection to remote server {self.remote_info} timedout."
                log.debug(error_msg)
                await self.broadcast_to_clients(f"\r{error_msg}\n\r")

            except Exception as e:
                error_msg = f"Warning: Connection to remote server {self.remote_info} unknown error: {e}."
                log.debug(error_msg)
                await self.broadcast_to_clients(f"\r{error_msg}\n\r")

            log.debug("end run loop")

    async def start_proxy(self):
        log.debug("Starting telnet proxy.")
        asyncio.create_task(self.handle_remote_server())
        self.server = await telnetlib3.create_server(
            host=self.listen_ip, port=self.listen_port,
            shell=self.handle_client
        )
        async with self.server:
            log.debug("Startup of telnet proxy complete.")
            await self.server.wait_closed()
        return self

    async def shutdown(self):
        log.debug(f"Set shutdown")
        self.closing = True
        if self.server:
            try:
                log.debug(f"Shuting down tcp listen port {self.remote_port}")
                self.server.close()
                await self.server.wait_closed()
            except Exception as e:
                log.debug(f"Failed to shutdown listen port: {self.remote_port}  {e}")
                
        for client in self.clients:
            try:
                try: 
                    client_info = client.get_extra_info('peername')
                except:
                    client_info = "Unknown"
                log.debug(f"Shuting down tcp session to {client_info}")
                client.close()
            except Exception as e:
                log.debug(f"Closing client connect {client_info} failed {e}")
        if self.remote_writer:
            try:
                self.remote_writer.close()
            except Exception as e:
                log.debug(f"Failed to shutdown listen port: {self.remote_info}  {e}")

        log.debug("No remaining work to do for shutdown.")



if __name__ == "__main__":

    async def main():
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
        )
        #pdb.set_trace()
        ## Example usage
        log.debug("Start proxy")

        try:
            #await asyncio.sleep(0)
            server = SFTelnetProxyMuxer(remote_server="10.1.14.22", remote_port=7058, listen_ip="0.0.0.0", listen_port=5010)
            await server.start_proxy()
        except OSError as e:
            log.debug(f"Can't start proxy: {e}")

        # To shut down the proxy
        # asyncio.run(proxy.shutdown())

    asyncio.run(main())
