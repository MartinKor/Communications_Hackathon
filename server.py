import socket
import struct
import threading
import time
import os

# Enable ANSI escape sequences on Windows
os.system("")

# ANSI color codes
COLOR = {
    "RESET": "\033[0m",
    "BOLD": "\033[1m",
    "RED": "\033[0;31m",
    "GREEN": "\033[0;32m",
    "YELLOW": "\033[0;33m",
    "BLUE": "\033[0;34m",
    "CYAN": "\033[0;36m",
    "PURPLE": "\033[0;35m",
}

BROADCAST_PORT = 13117             # The UDP port on which server broadcasts "offer" messages
OFFER_MAGIC_COOKIE = 0xabcddcba    # Magic cookie to identify valid offer messages
OFFER_MESSAGE_TYPE = 0x2           # Type of "offer" message (server -> client)
REQUEST_MESSAGE_TYPE = 0x3         # Type of "request" message (client -> server)
PAYLOAD_MESSAGE_TYPE = 0x4         # Type of "payload" message (server -> client)

BROADCAST_INTERVAL = 1.0           # Time interval between broadcast offers (in seconds)

def get_local_ip():
    """
    Get the local IP address of the server.
    Returns the local IP address as a string, or "127.0.0.1" if it cannot be determined.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

def broadcast_offers(stop_event, udp_port, tcp_port):
    """
    Broadcast "offer" messages to clients on the network.

    The "offer" packet contains:
      - Magic cookie (4 bytes)
      - Message type (1 byte) = 0x2
      - Server UDP port (2 bytes)
      - Server TCP port (2 bytes)
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)  # Enable broadcasting
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Allow address reuse
        while not stop_event.is_set():
            # Pack the "offer" packet
            offer_packet = struct.pack("!IbHH",
                                       OFFER_MAGIC_COOKIE,
                                       OFFER_MESSAGE_TYPE,
                                       udp_port,
                                       tcp_port)
            # Send the packet to the broadcast address
            sock.sendto(offer_packet, ("<broadcast>", BROADCAST_PORT))
            time.sleep(BROADCAST_INTERVAL)

def handle_tcp_client(conn, addr):
    """
    Handle an individual TCP client.
    1) Receive the requested size from the client.
    2) Send the requested number of bytes to the client.
    3) Close the connection after sending all data.
    """
    print(f"{COLOR['GREEN']}[TCP] Client connected from {addr}{COLOR['RESET']}")
    try:
        data = b""
        while True:
            chunk = conn.recv(1024)  # Receive data from the client
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break

        # Decode and validate the requested size
        requested_str = data.decode(errors='ignore').strip()
        try:
            requested_size = int(requested_str)
        except ValueError:
            print(f"{COLOR['RED']}[TCP] Error: client {addr} sent invalid size '{requested_str}'{COLOR['RESET']}")
            conn.close()
            return

        if requested_size <= 0:
            print(f"{COLOR['YELLOW']}[TCP] Ignoring non-positive requested size {requested_size} from {addr}{COLOR['RESET']}")
            conn.close()
            return

        print(f"{COLOR['GREEN']}[TCP] {addr} requested {requested_size} bytes{COLOR['RESET']}")
        bytes_sent = 0
        CHUNK_SIZE = 4096

        # Send the requested number of bytes
        while bytes_sent < requested_size:
            to_send = min(CHUNK_SIZE, requested_size - bytes_sent)
            conn.sendall(b'A' * to_send)  # Send 'A' as data
            bytes_sent += to_send

        print(f"{COLOR['BLUE']}[TCP] Finished sending {requested_size} bytes to {addr}{COLOR['RESET']}")
    except ConnectionResetError:
        print(f"{COLOR['RED']}[TCP] Connection reset by {addr}{COLOR['RESET']}")
    except Exception as e:
        print(f"{COLOR['RED']}[TCP] Error with {addr}: {e}{COLOR['RESET']}")
    finally:
        conn.close()

def handle_udp_client(server_socket, client_addr, requested_size):
    """
    Handle an individual UDP client.
    1) Send the requested size of data in packets with headers.
    2) Track the total number of packets sent.
    """
    print(f"{COLOR['CYAN']}[UDP] Handling {client_addr}, requested={requested_size} bytes{COLOR['RESET']}")
    if requested_size <= 0:
        print(f"{COLOR['YELLOW']}[UDP] Ignoring non-positive requested size {requested_size} from {client_addr}{COLOR['RESET']}")
        return

    CHUNK_SIZE = 1024
    total_segments = (requested_size + CHUNK_SIZE - 1) // CHUNK_SIZE  # Calculate total packets

    bytes_sent = 0
    for segment_num in range(total_segments):
        segment_size = min(CHUNK_SIZE, requested_size - bytes_sent)
        payload = b'B' * segment_size  # Data to send ('B' bytes)

        # Construct the packet header
        header = struct.pack("!IbQQ",
                             OFFER_MAGIC_COOKIE,
                             PAYLOAD_MESSAGE_TYPE,
                             total_segments,
                             segment_num)
        packet = header + payload
        try:
            server_socket.sendto(packet, client_addr)  # Send the packet
        except ConnectionResetError:
            print(f"{COLOR['RED']}[UDP] Connection reset by {client_addr}{COLOR['RESET']}")
            break
        except Exception as e:
            print(f"{COLOR['RED']}[UDP] Error sending to {client_addr}: {e}{COLOR['RESET']}")
            break
        bytes_sent += segment_size

    print(f"{COLOR['PURPLE']}[UDP] Done sending {bytes_sent} bytes in {total_segments} segments to {client_addr}{COLOR['RESET']}")

def udp_listener(stop_event, udp_socket):
    """
    Listen for client requests over UDP.
    For each request, create a new thread to handle the client.
    """
    while not stop_event.is_set():
        try:
            udp_socket.settimeout(1.0)  # Set a timeout for receiving
            data, addr = udp_socket.recvfrom(2048)  # Receive data from a client
        except socket.timeout:
            continue
        except Exception:
            continue

        if len(data) < 13:  # Ensure the packet has a valid size
            continue

        # Unpack the "request" packet
        magic_cookie, msg_type = struct.unpack("!Ib", data[:5])
        if magic_cookie != OFFER_MAGIC_COOKIE or msg_type != REQUEST_MESSAGE_TYPE:
            continue

        requested_size = struct.unpack("!Q", data[5:13])[0]

        # Create a thread to handle the client
        t = threading.Thread(
            target=handle_udp_client,
            args=(udp_socket, addr, requested_size),
            daemon=True
        )
        t.start()

def tcp_listener(stop_event, tcp_socket):
    """
    Listen for client connections over TCP.
    For each connection, create a new thread to handle the client.
    """
    while not stop_event.is_set():
        try:
            tcp_socket.settimeout(1.0)  # Set a timeout for accepting connections
            conn, addr = tcp_socket.accept()  # Accept a new connection
        except socket.timeout:
            continue
        except Exception:
            continue

        # Create a thread to handle the client
        t = threading.Thread(
            target=handle_tcp_client,
            args=(conn, addr),
            daemon=True
        )
        t.start()

def main():
    """
    Main server logic:
    1) Create and bind UDP and TCP sockets.
    2) Broadcast offers and listen for client connections.
    3) Handle client requests using threads.
    """
    print(f"{COLOR['BOLD']}Server starting up...{COLOR['RESET']}")

    stop_event = threading.Event()

    # Create an ephemeral UDP socket
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_socket.bind(("", 0))
    ephemeral_udp_port = udp_socket.getsockname()[1]

    # Create an ephemeral TCP socket
    tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_socket.bind(("", 0))
    ephemeral_tcp_port = tcp_socket.getsockname()[1]
    tcp_socket.listen(5)

    local_ip = get_local_ip()
    print(f"{COLOR['GREEN']}Server started, listening on IP address {local_ip}{COLOR['RESET']}")
    print(f"{COLOR['GREEN']}Using ephemeral UDP port={ephemeral_udp_port}, TCP port={ephemeral_tcp_port}{COLOR['RESET']}")

    # Start broadcasting offers
    broadcaster = threading.Thread(
        target=broadcast_offers,
        args=(stop_event, ephemeral_udp_port, ephemeral_tcp_port),
        daemon=True
    )
    broadcaster.start()

    # Start UDP listener
    udp_thread = threading.Thread(
        target=udp_listener,
        args=(stop_event, udp_socket),
        daemon=True
    )
    udp_thread.start()

    # Start TCP listener
    tcp_thread = threading.Thread(
        target=tcp_listener,
        args=(stop_event, tcp_socket),
        daemon=True
    )
    tcp_thread.start()

    try:
        stop_event.wait()  # Wait until interrupted
    except KeyboardInterrupt:
        print(f"{COLOR['RED']}Shutting down server...{COLOR['RESET']}")
        stop_event.set()
    finally:
        time.sleep(2)
        udp_socket.close()
        tcp_socket.close()

if __name__ == "__main__":
    main()
