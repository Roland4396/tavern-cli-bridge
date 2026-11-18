"""Private TCP-to-Unix relay with no request, read, write or drain deadlines.
HTTP authentication, paths and body/capacity bounds remain in the Unix app.
Deploy only on the private Docker network, never publish this TCP port.
"""
import argparse
import asyncio
import json
import signal
import time

class Relay:
    def __init__(self, path, limit=48):
        self.path = path
        self.limit = limit
        self.active = set()

    async def handle(self, reader, writer):
        if len(self.active) >= self.limit:
            writer.write(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            writer.close()
            return
        task = asyncio.current_task(); self.active.add(task)
        started = time.monotonic(); upstream = None; pumps = []; result = 'closed'
        print(json.dumps({'event':'connection_open','active':len(self.active)}), flush=True)
        async def pump(source, dest):
            while data := await source.read(65536):
                dest.write(data)
                await dest.drain()
        try:
            source, upstream = await asyncio.open_unix_connection(self.path, limit=65536)
            pumps = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(source, writer))]
            # A disconnect in either direction closes the peer immediately, even
            # if the other pump is blocked on a silent read or backpressure.
            done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for finished in done: finished.result()
        except (OSError, ConnectionError):
            result = 'transport_closed'
        finally:
            for p in pumps: p.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            # Flush all queued final bytes; never drop a tail at upstream EOF.
            for w in (writer, upstream):
                if w: w.close()
            await asyncio.gather(*(w.wait_closed() for w in (writer, upstream) if w), return_exceptions=True)
            self.active.discard(task)
            print(json.dumps({'event':'connection_close','result':result,'seconds':round(time.monotonic()-started,3),'active':len(self.active)}), flush=True)

async def run(host, port, path):
    relay = Relay(path); stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stopped.set)
    server = await asyncio.start_server(relay.handle, host, port, limit=65536)
    print(json.dumps({'event':'ready','transport':'private_tcp_to_unix','request_timeout':None,'read_timeout':None,'write_timeout':None,'drain_timeout':None}), flush=True)
    await stopped.wait()
    server.close(); await server.wait_closed()
    while relay.active: await asyncio.gather(*list(relay.active), return_exceptions=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--socket', default='/run/agy/api.sock')
    args = parser.parse_args()
    asyncio.run(run(args.host, args.port, args.socket))

if __name__ == '__main__': main()
