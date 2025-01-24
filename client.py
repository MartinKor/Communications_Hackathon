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
OFFER_MAGIC_COOKIE = 0xabcddcba    # Must match server's magic cookie
OFFER_MESSAGE_TYPE = 0x2           # "Offer" message (server -> client)
REQUEST_MESSAGE_TYPE = 0x3         # "Request" message (client -> server)
PAYLOAD_MESSAGE_TYPE = 0x4         # "Payload" message (server -> client)

DISCOVERY_TIMEOUT = 10.0           # How many seconds we wait for a broadcast offer

def get_positive_int(prompt):
    """
    Repeatedly prompt the user for an integer > 0.
    If the input is invalid or <= 0, prints an error and prompts again.
    Returns the valid integer input.
    """
    while True:
        s = input(f"{COLOR['CYAN']}{prompt}{COLOR['RESET']}").strip()
        try:
            val = int(s)
            if val <= 0:
                print(f"{COLOR['RED']}Value must be a positive integer. Please try again.{COLOR['RESET']}")
                continue
            return val
        except ValueError:
            print(f"{COLOR['RED']}Invalid integer. Please try again.{COLOR['RESET']}")

def discover_server():
    """
    Bind to port BROADCAST_PORT (13117) to listen for server "offer" broadcasts.
    Returns (server_ip, server_udp_port, server_tcp_port) if found within DISCOVERY_TIMEOUT,
    else (None, None, None).

    The "offer" packet is expected to have:
      - Magic cookie (4 bytes)
      - Message type (1 byte) = 0x2
      - Server UDP port (2 bytes)
      - Server TCP port (2 bytes)
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", BROADCAST_PORT))
    sock.settimeout(0.5)

    print(f"{COLOR['GREEN']}Listening for server offers on port {BROADCAST_PORT}...{COLOR['RESET']}")
    start_time = time.time()
    while True:
        if time.time() - start_time > DISCOVERY_TIMEOUT:
            # Stop listening after timeout
            sock.close()
            return (None, None, None)
        try:
            data, addr = sock.recvfrom(1024)  # Receive broadcast packet
        except socket.timeout:
            continue  # Retry until timeout
        except Exception:
            continue

        if len(data) < 9:
            continue  # Ignore malformed packets

        # Unpack the first 5 bytes for magic cookie and message type
        magic_cookie, msg_type = struct.unpack("!Ib", data[:5])
        if magic_cookie == OFFER_MAGIC_COOKIE and msg_type == OFFER_MESSAGE_TYPE:
            # Unpack UDP and TCP ports from the next 4 bytes
            server_udp_port, server_tcp_port = struct.unpack("!HH", data[5:9])
            sock.close()
            return (addr[0], server_udp_port, server_tcp_port)

def tcp_transfer(server_ip, tcp_port, requested_size, index=1):
    """
    Perform one TCP transfer:
    1) Connect to the server's tcp_port
    2) Send the requested_size in ASCII plus newline
    3) Receive data until we've read that many bytes or the server closes
    """
    start_time = time.time()
    total_received = 0
    print(f"{COLOR['BLUE']}[TCP transfer #{index}] Connecting to {server_ip}:{tcp_port}...{COLOR['RESET']}")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((server_ip, tcp_port))  # Connect to server
            s.sendall(f"{requested_size}\n".encode())  # Send requested size

            # Receive data until requested_size is reached
            while total_received < requested_size:
                chunk = s.recv(4096)
                if not chunk:
                    break  # Server closed connection
                total_received += len(chunk)
    except Exception as e:
        print(f"{COLOR['RED']}[TCP transfer #{index}] Error: {e}{COLOR['RESET']}")

    # Calculate elapsed time and speed
    elapsed = max(0.000001, time.time() - start_time)
    speed_bps = (total_received * 8) / elapsed
    print(f"{COLOR['GREEN']}[TCP transfer #{index}] finished, total time: {elapsed:.2f} s, "
          f"total speed: {speed_bps:.2f} bits/s{COLOR['RESET']}")

def udp_transfer(server_ip, udp_port, requested_size, index=1):
    """
    Perform one UDP transfer:
    1) Send a 'request' packet containing the requested_size
    2) Receive multiple payload packets, each with a header + data
    3) Stop once no packets arrive for ~1 second
    4) Calculate approximate speed and packet success rate
    """
    print(f"{COLOR['BLUE']}[UDP transfer #{index}] Connecting to {server_ip}:{udp_port}...{COLOR['RESET']}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)

    # Construct request packet: cookie + message type=0x3 + file size (8 bytes)
    req_packet = struct.pack("!IbQ", OFFER_MAGIC_COOKIE, REQUEST_MESSAGE_TYPE, requested_size)
    try:
        sock.sendto(req_packet, (server_ip, udp_port))  # Send request packet
    except Exception as e:
        print(f"{COLOR['RED']}[UDP transfer #{index}] Error sending request: {e}{COLOR['RESET']}")
        sock.close()
        return

    start_time = time.time()
    bytes_received = 0
    packets_received = 0
    total_segments = 0

    last_packet_time = time.time()
    while True:
        try:
            data, addr = sock.recvfrom(1500)  # Receive data
        except socket.timeout:
            if time.time() - last_packet_time > 1.0:
                break  # No packets received recently, stop transfer
            continue

        if not data or len(data) < 21:
            continue  # Ignore malformed packets

        # Unpack the header: cookie (4), msg_type (1), total_segments (8), current_segment (8)
        cookie, msg_type, total_segments, current_segment = struct.unpack("!IbQQ", data[:21])
        if cookie != OFFER_MAGIC_COOKIE or msg_type != PAYLOAD_MESSAGE_TYPE:
            continue

        payload = data[21:]  # Extract payload
        packets_received += 1
        bytes_received += len(payload)
        last_packet_time = time.time()

    # Calculate elapsed time, speed, and success rate
    elapsed = max(0.000001, time.time() - start_time)
    speed_bps = (bytes_received * 8) / elapsed
    pct_received = (packets_received / total_segments) * 100 if total_segments > 0 else 100.0

    print(f"{COLOR['GREEN']}[UDP transfer #{index}] finished, total time: {elapsed:.2f} s, "
          f"total speed: {speed_bps:.2f} bits/s, "
          f"percentage of packets received successfully: {pct_received:.1f}%{COLOR['RESET']}")
    sock.close()

def main():
    """
    Main entry point for the client.
    1) Prompt the user for file size, number of TCP connections, and UDP connections.
    2) Discover a server broadcasting offers.
    3) Perform the requested TCP/UDP transfers in parallel threads.
    4) Print the speed stats for each transfer.
    """
    print(f"{COLOR['BOLD']}Client started, listening for server offer requests...{COLOR['RESET']}")

    # Get input parameters
    requested_size = get_positive_int("Enter file size in bytes (e.g. 1000000): ")
    num_tcp = get_positive_int("Enter number of TCP connections (e.g. 1): ")
    num_udp = get_positive_int("Enter number of UDP connections (e.g. 2): ")

    while True:
        print(f"{COLOR['YELLOW']}Looking for server (broadcast discovery on port {BROADCAST_PORT})...{COLOR['RESET']}")
        server_ip, server_udp_port, server_tcp_port = discover_server()
        if server_ip is None:
            print(f"{COLOR['RED']}No offers received. Retrying...{COLOR['RESET']}")
            continue

        print(f"{COLOR['GREEN']}Discovered server at {server_ip}, ephemeral UDP={server_udp_port}, TCP={server_tcp_port}{COLOR['RESET']}")

        threads = []
        # Start TCP transfers
        for i in range(num_tcp):
            t = threading.Thread(target=tcp_transfer, args=(server_ip, server_tcp_port, requested_size, i+1))
            t.start()
            threads.append(t)

        # Start UDP transfers
        for i in range(num_udp):
            t = threading.Thread(target=udp_transfer, args=(server_ip, server_udp_port, requested_size, i+1))
            t.start()
            threads.append(t)

        # Wait for all threads to finish
        for t in threads:
            t.join()

        print(f"{COLOR['CYAN']}All transfers complete. Restarting discovery...{COLOR['RESET']}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{COLOR['RED']}Client shutting down.{COLOR['RESET']}")
