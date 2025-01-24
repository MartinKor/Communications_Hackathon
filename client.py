
import socket
import struct
import threading
import time


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
        s = input(prompt).strip()
        try:
            val = int(s)
            if val <= 0:
                print("Value must be a positive integer. Please try again.")
                continue
            return val
        except ValueError:
            print("Invalid integer. Please try again.")

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

    start_time = time.time()
    while True:
        if time.time() - start_time > DISCOVERY_TIMEOUT:
            sock.close()
            return (None, None, None)
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            continue
        except Exception:
            continue

        if len(data) < 9:
            continue

        magic_cookie, msg_type = struct.unpack("!Ib", data[:5])
        if magic_cookie == OFFER_MAGIC_COOKIE and msg_type == OFFER_MESSAGE_TYPE:
            # The next 4 bytes are server_udp_port (2 bytes) + server_tcp_port (2 bytes)
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
    print(f"[TCP transfer #{index}] Connecting to {server_ip}:{tcp_port}...")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((server_ip, tcp_port))
            s.sendall(f"{requested_size}\n".encode())

            while total_received < requested_size:
                chunk = s.recv(4096)
                if not chunk:
                    # Server closed or we are done
                    break
                total_received += len(chunk)
    except Exception as e:
        print(f"[TCP transfer #{index}] Error: {e}")

    end_time = time.time()
    elapsed = max(0.000001, end_time - start_time)
    speed_bps = (total_received * 8) / elapsed
    print(f"[TCP transfer #{index}] finished, total time: {elapsed:.2f} s, "
          f"total speed: {speed_bps:.2f} bits/s")

def udp_transfer(server_ip, udp_port, requested_size, index=1):
    """
    Perform one UDP transfer:
    1) Send a 'request' packet containing the requested_size
    2) Receive multiple payload packets, each with a header + data
    3) Stop once no packets arrive for ~1 second
    4) Calculate approximate speed and packet success rate
    """
    print(f"[UDP transfer #{index}] Connecting to {server_ip}:{udp_port}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)

    # Construct request packet: cookie + message type=0x3 + file size (8 bytes)
    req_packet = struct.pack("!IbQ", OFFER_MAGIC_COOKIE, REQUEST_MESSAGE_TYPE, requested_size)
    try:
        sock.sendto(req_packet, (server_ip, udp_port))
    except Exception as e:
        print(f"[UDP transfer #{index}] Error sending request: {e}")
        sock.close()
        return

    start_time = time.time()
    bytes_received = 0
    packets_received = 0
    total_segments = 0

    last_packet_time = time.time()
    while True:
        try:
            data, addr = sock.recvfrom(1500)
        except socket.timeout:
            if time.time() - last_packet_time > 1.0:
                break
            continue

        # If the packet is too small, ignore
        if not data or len(data) < 21:
            continue

        # Unpack the header: cookie (4), msg_type (1), total_segments (8), current_segment (8)
        cookie, msg_type, total_segments, current_segment = struct.unpack("!IbQQ", data[:21])
        if cookie != OFFER_MAGIC_COOKIE or msg_type != PAYLOAD_MESSAGE_TYPE:
            continue

        payload = data[21:]
        packets_received += 1
        bytes_received += len(payload)
        last_packet_time = time.time()

    elapsed = max(0.000001, time.time() - start_time)
    speed_bps = (bytes_received * 8) / elapsed

    pct_received = 100.0
    if total_segments > 0:
        pct_received = (packets_received / total_segments) * 100

    print(f"[UDP transfer #{index}] finished, total time: {elapsed:.2f} s, "
          f"total speed: {speed_bps:.2f} bits/s, "
          f"percentage of packets received successfully: {pct_received:.1f}%")
    sock.close()

def main():
    """
    Main entry point for the client.
    1) Prompt the user for file size, number of TCP connections, number of UDP connections.
    2) Continuously listen for a server "offer" broadcast. If found, connect to that server's
       ephemeral ports for speed testing.
    3) Perform the requested TCP/UDP transfers in parallel threads.
    4) Print the speed stats for each transfer, then loop back to discover another server.
    5) Press Ctrl-C to quit the client.
    """
    print("Client started, listening for offer requests...")

    requested_size = get_positive_int("Enter file size in bytes (e.g. 1000000): ")
    num_tcp = get_positive_int("Enter number of TCP connections (e.g. 1): ")
    num_udp = get_positive_int("Enter number of UDP connections (e.g. 2): ")

    while True:
        print("Looking for server (broadcast discovery on port 13117)...")
        server_ip, server_udp_port, server_tcp_port = discover_server()
        if server_ip is None:
            print("No offers received. Going back to looking for a server...\n")
            continue

        print(f"Discovered server at {server_ip}, ephemeral UDP={server_udp_port}, TCP={server_tcp_port}")

        threads = []
        for i in range(num_tcp):
            t = threading.Thread(target=tcp_transfer, args=(server_ip, server_tcp_port, requested_size, i+1))
            t.start()
            threads.append(t)

        for i in range(num_udp):
            t = threading.Thread(target=udp_transfer, args=(server_ip, server_udp_port, requested_size, i+1))
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

        print("All transfers complete, listening for offer requests again...\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nClient shutting down.")
