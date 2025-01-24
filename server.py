
import socket
import threading
import struct
import time

BROADCAST_PORT = 13117             # Port on which we broadcast "offer" packets
OFFER_MAGIC_COOKIE = 0xabcddcba    # Magic cookie for identifying valid packets
OFFER_MESSAGE_TYPE = 0x2           # "Offer" message (server -> client)
REQUEST_MESSAGE_TYPE = 0x3         # "Request" message (client -> server)
PAYLOAD_MESSAGE_TYPE = 0x4         # "Payload" message (server -> client)

BROADCAST_INTERVAL = 1.0           # How often (in seconds) to broadcast offers

def get_local_ip():
    """
    Attempt to retrieve the local IP address used to connect to the outside world.
    This is used primarily for logging or display. If we fail, return '127.0.0.1'.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connect to a well-known public address (e.g. 8.8.8.8) so we can query our local IP.
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

def broadcast_offers(stop_event, udp_port, tcp_port):
    """
    Broadcast "offer" messages on the well-known BROADCAST_PORT every BROADCAST_INTERVAL seconds.
    Each offer message includes:
       - Magic cookie (4 bytes)
       - Offer message type (1 byte)
       - Server UDP port (2 bytes)
       - Server TCP port (2 bytes)
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        while not stop_event.is_set():
            offer_packet = struct.pack(
                "!IbHH",
                OFFER_MAGIC_COOKIE,
                OFFER_MESSAGE_TYPE,
                udp_port,
                tcp_port
            )
            sock.sendto(offer_packet, ("<broadcast>", BROADCAST_PORT))

            time.sleep(BROADCAST_INTERVAL)

def handle_tcp_client(conn, addr):
    """
    Handle a single TCP client connection.
    1) Reads a line from the client containing the requested file size (in bytes).
    2) Validates the requested size; if invalid or non-positive, ignore.
    3) Sends exactly that many bytes back to the client in chunks.
    """
    print(f"[TCP] Client connected from {addr}")
    try:
        data = b""
        # Read until newline or the client closes
        while True:
            chunk = conn.recv(1024)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break

        requested_str = data.decode(errors='ignore').strip()
        try:
            requested_size = int(requested_str)
        except ValueError:
            print(f"[TCP] Error: client {addr} sent invalid size '{requested_str}'")
            conn.close()
            return

        if requested_size <= 0:
            print(f"[TCP] Ignoring non-positive requested size {requested_size} from {addr}")
            conn.close()
            return

        # We have a valid, positive requested_size. Send that many bytes (all 'A's).
        print(f"[TCP] {addr} requested {requested_size} bytes")
        bytes_sent = 0
        CHUNK_SIZE = 4096
        while bytes_sent < requested_size:
            to_send = min(CHUNK_SIZE, requested_size - bytes_sent)
            conn.sendall(b'A' * to_send)
            bytes_sent += to_send

        print(f"[TCP] Finished sending {requested_size} bytes to {addr}")
    except ConnectionResetError:
        print(f"[TCP] Connection reset by {addr}")
    except Exception as e:
        print(f"[TCP] Error with {addr}: {e}")
    finally:
        conn.close()

def handle_udp_client(server_socket, client_addr, requested_size):
    """
    Handle a single UDP request from client_addr.
    1) Validate requested_size; ignore if non-positive.
    2) Send the requested_size bytes in multiple packets, each containing a header:
         Magic cookie (4 bytes)
         Payload message type (1 byte) = 0x4
         Total segments (8 bytes)
         Current segment index (8 bytes)
       Followed by the actual data chunk.
    """
    print(f"[UDP] Handling {client_addr}, requested={requested_size} bytes")
    if requested_size <= 0:
        print(f"[UDP] Ignoring non-positive requested size {requested_size} from {client_addr}")
        return

    CHUNK_SIZE = 1024
    total_segments = (requested_size + CHUNK_SIZE - 1) // CHUNK_SIZE

    bytes_sent = 0
    for segment_num in range(total_segments):
        segment_size = min(CHUNK_SIZE, requested_size - bytes_sent)
        payload = b'B' * segment_size

        header = struct.pack(
            "!IbQQ",
            OFFER_MAGIC_COOKIE,
            PAYLOAD_MESSAGE_TYPE,
            total_segments,
            segment_num
        )
        packet = header + payload
        try:
            server_socket.sendto(packet, client_addr)
        except ConnectionResetError:
            print(f"[UDP] Connection reset by {client_addr}")
            break
        except Exception as e:
            print(f"[UDP] Error sending to {client_addr}: {e}")
            break

        bytes_sent += segment_size

    print(f"[UDP] Done sending {bytes_sent} bytes in {total_segments} segments to {client_addr}")

def udp_listener(stop_event, udp_socket):
    """
    Continuously listens on udp_socket for incoming 'request' packets (message type=0x3).
    If a valid request is received, spawns a new thread to handle it via handle_udp_client.
    """
    while not stop_event.is_set():
        try:
            udp_socket.settimeout(1.0)
            data, addr = udp_socket.recvfrom(2048)
        except socket.timeout:
            continue
        except Exception:
            continue

        if len(data) < 13:
            # Not enough bytes for cookie(4) + type(1) + requested_size(8)
            continue

        magic_cookie, msg_type = struct.unpack("!Ib", data[:5])
        if magic_cookie != OFFER_MAGIC_COOKIE or msg_type != REQUEST_MESSAGE_TYPE:
            continue

        requested_size = struct.unpack("!Q", data[5:13])[0]

        t = threading.Thread(
            target=handle_udp_client,
            args=(udp_socket, addr, requested_size),
            daemon=True
        )
        t.start()

def tcp_listener(stop_event, tcp_socket):
    """
    Continuously accept new TCP connections from clients.
    For each connection, spawn a new thread that calls handle_tcp_client.
    """
    while not stop_event.is_set():
        try:
            tcp_socket.settimeout(1.0)
            conn, addr = tcp_socket.accept()
        except socket.timeout:
            continue
        except Exception:
            continue

        t = threading.Thread(
            target=handle_tcp_client,
            args=(conn, addr),
            daemon=True
        )
        t.start()

def main():
    """
    Main entry point for the server.
    1) Create ephemeral UDP and TCP sockets, and retrieve their ports.
    2) Print local IP and ephemeral ports for reference.
    3) Start background threads for:
         - Broadcasting offers
         - Listening for UDP requests
         - Listening for TCP connections
    4) Wait until Ctrl-C, then shutdown gracefully.
    """
    print("Server starting up...")

    stop_event = threading.Event()

    # 1) Create the UDP socket for speed-test requests
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_socket.bind(("", 0))
    ephemeral_udp_port = udp_socket.getsockname()[1]

    # 2) Create the TCP socket for speed-test requests
    tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_socket.bind(("", 0))
    ephemeral_tcp_port = tcp_socket.getsockname()[1]
    tcp_socket.listen(5)

    local_ip = get_local_ip()
    print(f"Server started, listening on IP address {local_ip}")
    print(f"Using ephemeral UDP port={ephemeral_udp_port}, TCP port={ephemeral_tcp_port}")

    # 3) Start background threads
    broadcaster = threading.Thread(
        target=broadcast_offers,
        args=(stop_event, ephemeral_udp_port, ephemeral_tcp_port),
        daemon=True
    )
    broadcaster.start()

    udp_thread = threading.Thread(
        target=udp_listener,
        args=(stop_event, udp_socket),
        daemon=True
    )
    udp_thread.start()

    tcp_thread = threading.Thread(
        target=tcp_listener,
        args=(stop_event, tcp_socket),
        daemon=True
    )
    tcp_thread.start()

    # 4) Wait for Ctrl-C to stop
    try:
        stop_event.wait()  # Blocks until event is set or KeyboardInterrupt
    except KeyboardInterrupt:
        print("Shutting down server...")
        stop_event.set()
    finally:
        time.sleep(2)
        udp_socket.close()
        tcp_socket.close()

if __name__ == "__main__":
    main()
