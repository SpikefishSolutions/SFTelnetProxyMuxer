import socket
import asyncio
import telnetlib3
import pdb
import logging


# Configure logging
logging.basicConfig(
    level=logging.DEBUG,  # Set the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    format='%(asctime)s - %(levelname)s - %(message)s (%(filename)s:%(lineno)d)',
    datefmt='%Y-%m-%d %H:%M:%S'
)

class SFTelnetProxyMuxer:
    def __init__(self, remote_ip, remote_port, listen_ip, listen_port):
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.remote_info = f"('{self.remote_ip}', {self.remote_port})"
        self.listen_ip = listen_ip
        self.listen_port = listen_port
        self.clients = set()
        self.server = None
        self.remote_reader = None
        self.remote_writer = None
        self.lock = asyncio.Lock()  # Lock for coordinating access to the remote server
        # Telnet protocol constants
        self.IAC = b"\xff" # Interpret as Command
        # Telnet NOP command. Will be used as a heartbeat to clients.
        self.NOP = b"\xf1"
        # Telnet Are You There
        self.AYT = b"\xf6"

        logging.debug("TCPProxy init complete")

    async def handle_client(self, reader, writer):
        client_info = writer.get_extra_info('peername')
        sock = writer.get_extra_info('socket') 
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        logging.debug(f"New client connected: {client_info}")
        self.clients.add(writer)
        #logging.debug(f"Write idle: {writer.protocol.idle}")

        try:
            await asyncio.sleep(1)
            while True:
                try:
                    # Set a timeout for the read operation, without should the socket closes after timeout.
                    data = await asyncio.shield(asyncio.wait_for(reader.read((4*1024*1024)), timeout=2.0))
                    if not data:
                        logging.debug(f"No data. Not sure if this is possible.")
                        break
                    if reader.at_eof():
                        logging.info(f"Client {client_info} closed tcp session with eof.")
                        writer.close()
                        self.clients.discard(writer)
                        break

                    async with self.lock:
                        if self.remote_writer is not None:
                            logging.debug(f"Sending data from from client {client_info} to server {self.remote_info}")
                            self.remote_writer.write(data)
                            await self.remote_writer.drain()
                            continue
                           
                except asyncio.TimeoutError:
                    logging.warning(f"No data read from {client_info}, send heartbeat to test client socket.")
                    try:
                        logging.warning(f"Heatbeat: Are you there {client_info}?")
                        #pdb.set_trace()
                        writer.send_iac(self.IAC + self.NOP)
                        await writer.drain()
                        continue
                    except asyncio.TimeoutError:
                        logging.warning(f"Heatbeat: No reply from {client_info}, closing socket.")
                        writer.close()
                        self.clients.discard(writer)
                        break 
                    except Exception as e:
                        logging.warning(f"Heateat: Unknown error from {client_info}, closing socket. Exeption {e}")
                        writer.close()
                        self.clients.discard(writer)
                        break 
                    finally:
                        logging.warning(f"Heatbeat: {client_info} Yes I am.")
                except Exception as e:
                    logging.exception(f"Error in handling data from client {client_info}:")
                    writer.close()
                    self.clients.discard(writer)
                    break

        except Exception as e:
            logging.exception(f"Error in managing client {client_info}: {e}")

        finally:
            # Safely remove the writer from clients set and close the connection
            writer.close()
            self.clients.discard(writer)
            logging.debug(f"Client {client_info} disconnected. Remaining clients: {len(list(self.clients))}")
            logging.debug(f"Connection with client {client_info} closed.")


    async def broadcast_to_clients(self, data):
        if not self.clients:
            logging.debug(f"Warning: No clients connected, ignoring data.")
            return 
            
        for writer in set(self.clients):
            client_info = writer.get_extra_info('peername')
            try:
                #logging.debug(f"Clients connected: {writer}, sending data: {data}")
                writer.write(data)
                await asyncio.wait_for(writer.drain(), timeout=2.0)
            except Exception as e:
                logging.debug(f"Lost connection to client {client_info}")
                writer.close()
                self.clients.discard(writer)

    async def handle_remote_server(self):
        logging.debug("Start handler for remote server")
        while True:
            await asyncio.sleep(1)
            try:
                self.remote_reader, self.remote_writer = await telnetlib3.open_connection(
                    host=self.remote_ip, port=self.remote_port
                )
                sock = self.remote_writer.get_extra_info('socket') 
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                while True:
                    
                    try:
                        #data = await self.remote_reader.read((4*1024*1024))
                        data = await asyncio.shield(asyncio.wait_for(self.remote_reader.read((4*1024*1024)), timeout=2.0))
                        if self.remote_reader.at_eof():
                            logging.info(f"Remote server {self.remote_info} closed tcp session with eof.")
                            break
                    except asyncio.TimeoutError:
                        logging.warning(f"No data from server {self.remote_info}, send heartbeat to test socket.")
                        try:
                            logging.warning(f"Heatbeat: Are you there {self.remote_info}?")
                            #pdb.set_trace()
                            #self.remote_writer.write("")
                            await self.remote_writer.drain()
                            continue
                        except Exception as e:
                            logging.warning(f"Heateat: Unknown error from {self.remote_info}, closing socket. Exeption {e}")
                            self.remote_writer.close()
                            break
                        finally:
                            logging.warning(f"Heatbeat: {self.remote_info} Yes I am.")

                    except Exception as e:
                        logging.debug("Failed to read socket data exception: {e}")
                        break
                    #if not self.clients:
                    #    logging.debug("No clients connected, but console data found. Skipping.")
                    #    continue
                    #logging.debug("Sending data to clients data: {data}")
                    await self.broadcast_to_clients(data)
            except ConnectionRefusedError as e:
                error_msg = f"Warning: Connection to remote server {self.remote_info} refused."
                logging.debug(error_msg)
                await self.broadcast_to_clients(f"\r{error_msg}\n\r")

            except TimeoutError as e:
                error_msg = f"Warning: Connection to remote server {self.remote_info} timedout."
                logging.debug(error_msg)
                await self.broadcast_to_clients(f"\r{error_msg}\n\r")

            except Exception as e:
                error_msg = f"Warning: Connection to remote server {self.remote_info} unknown error: {e}."
                logging.debug(error_msg)
                await self.broadcast_to_clients(f"\r{error_msg}\n\r")

    async def start_proxy(self):
        logging.debug("Starting telnet proxy.")
        asyncio.create_task(self.handle_remote_server())
        self.server = await telnetlib3.create_server(
            host=self.listen_ip, port=self.listen_port,
            shell=self.handle_client
        )
        async with self.server:
            logging.debug("Startup of telnet proxy complete.")
            await self.server.wait_closed()

    async def shutdown(self):
        # [shutdown method implementation remains the same]
        logging.debug("Debug message")
        pass

if __name__ == "__main__":

    ## Example usage
    logging.debug("Start proxy")
    proxy = SFTelnetProxyMuxer(remote_ip='127.0.0.1', remote_port=7000, listen_ip='0.0.0.0', listen_port=8888)
    try:
        asyncio.wait_for(asyncio.run(proxy.start_proxy()), timeout=30)
    except OSError as e:
        logging.debug(f"Can't start proxy: {e}")

    # To shut down the proxy
    # asyncio.run(proxy.shutdown())

