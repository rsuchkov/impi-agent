package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

// fakeChrome answers like Chrome's DevTools endpoint: it advertises its own
// loopback authority and accepts a WebSocket upgrade.
func fakeChrome(t *testing.T) (address string, started func() int) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })

	addr := listener.Addr().String()
	connections := make(chan struct{}, 100)

	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			connections <- struct{}{}
			go func() {
				defer conn.Close()
				reader := bufio.NewReader(conn)
				for {
					request, err := http.ReadRequest(reader)
					if err != nil {
						return
					}
					if strings.EqualFold(request.Header.Get("Upgrade"), "websocket") {
						fmt.Fprint(conn, "HTTP/1.1 101 Switching Protocols\r\n\r\n")
						// Echo, so the test can prove the tunnel is bidirectional.
						io.Copy(conn, reader)
						return
					}
					body := fmt.Sprintf(
						`{"Browser":"Chrome/151","webSocketDebuggerUrl":"ws://%s/devtools/browser/abc"}`,
						addr)
					// Reported as a header, not in the body: the body is subject
					// to the authority rewrite, which would mask what was seen.
					fmt.Fprintf(conn, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"+
						"X-Seen-Host: %s\r\nX-Seen-Origin: %s\r\nContent-Length: %d\r\n\r\n%s",
						request.Host, request.Header.Get("Origin"), len(body), body)
				}
			}()
		}
	}()

	return addr, func() int { return len(connections) }
}

// startRelay wires a proxy in front of a browser whose "launch" is a no-op,
// because the fake Chrome is already listening.
func startRelay(t *testing.T, upstream string, idle time.Duration) string {
	t.Helper()
	browser := NewBrowser([]string{"/bin/sh", "-c", "sleep 300"}, upstream, idle, 5*time.Second)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(func() {
		cancel()
		browser.Stop("test over")
	})
	go NewProxy(browser, upstream).Serve(ctx, listener)
	return listener.Addr().String()
}

func TestRewritesWebSocketURLToTheAddressTheClientUsed(t *testing.T) {
	upstream, _ := fakeChrome(t)
	relay := startRelay(t, upstream, time.Minute)

	response, err := http.Get("http://" + relay + "/json/version")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()

	var payload struct {
		WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
	}
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}

	want := "ws://" + relay + "/devtools/browser/abc"
	if payload.WebSocketDebuggerURL != want {
		t.Errorf("webSocketDebuggerUrl = %q, want %q", payload.WebSocketDebuggerURL, want)
	}
	// Chrome rejects a Host that is neither localhost nor an IP.
	if seen := response.Header.Get("X-Seen-Host"); seen != upstream {
		t.Errorf("upstream saw Host %q, want the loopback authority %q", seen, upstream)
	}
}

func TestKeepAliveLetsTheUpgradeReuseTheConnection(t *testing.T) {
	// The Python original closed after one response, so the WebSocket upgrade
	// landed on a dead socket and clients reported "socket hang up".
	upstream, _ := fakeChrome(t)
	relay := startRelay(t, upstream, time.Minute)

	conn, err := net.Dial("tcp", relay)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	fmt.Fprintf(conn, "GET /json/version HTTP/1.1\r\nHost: %s\r\nConnection: keep-alive\r\n\r\n", relay)
	reader := bufio.NewReader(conn)
	if _, err := http.ReadResponse(reader, nil); err != nil {
		t.Fatalf("first response: %v", err)
	}

	// Same socket, now upgrading — exactly what Playwright does.
	fmt.Fprintf(conn, "GET /devtools/browser/abc HTTP/1.1\r\nHost: %s\r\n"+
		"Upgrade: websocket\r\nConnection: Upgrade\r\n"+
		"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n", relay)

	status, err := reader.ReadString('\n')
	if err != nil {
		t.Fatalf("upgrade on the reused connection failed: %v", err)
	}
	if !strings.Contains(status, "101") {
		t.Fatalf("expected 101, got %q", status)
	}
}

func TestStripsOriginSoChromeDoesNotRefuseTheUpgrade(t *testing.T) {
	upstream, _ := fakeChrome(t)
	relay := startRelay(t, upstream, time.Minute)

	request, _ := http.NewRequest("GET", "http://"+relay+"/json/version", nil)
	request.Header.Set("Origin", "http://evil.example")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()

	if seen := response.Header.Get("X-Seen-Origin"); seen != "" {
		t.Errorf("Origin %q reached the browser; Chrome would refuse the upgrade", seen)
	}
}

func TestHealthEndpointDoesNotStartTheBrowser(t *testing.T) {
	// A health check that reached /json/version would start Chrome on every
	// probe, which would keep the container permanently un-idle.
	upstream, _ := fakeChrome(t)
	browser := NewBrowser([]string{"/bin/sh", "-c", "sleep 300"}, upstream, time.Minute, 5*time.Second)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer func() {
		cancel()
		browser.Stop("test over")
	}()
	go NewProxy(browser, upstream).Serve(ctx, listener)

	response, err := http.Get("http://" + listener.Addr().String() + HealthPath)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		t.Errorf("status = %d, want 200", response.StatusCode)
	}
	if browser.Running() {
		t.Error("health check started the browser")
	}
}

func TestAnOpenConnectionKeepsTheBrowserOffTheIdleTimer(t *testing.T) {
	// What makes the idle timeout safe to have at all. A CDP client holds its
	// WebSocket open for the whole session, and that connection is the thing
	// counted — so the browser cannot be stopped underneath an agent that is
	// mid-task and merely thinking between commands. It becomes idle when the
	// client goes away, not when it goes quiet.
	upstream, _ := fakeChrome(t)
	relay := startRelay(t, upstream, 50*time.Millisecond)

	client, err := net.Dial("tcp", relay)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	fmt.Fprintf(client, "GET /json/version HTTP/1.1\r\nHost: %s\r\n\r\n", relay)
	if _, err := http.ReadResponse(bufio.NewReader(client), nil); err != nil {
		t.Fatal(err)
	}

	// Several idle timeouts' worth of doing nothing on an open connection.
	time.Sleep(300 * time.Millisecond)
	if !browserOf(t, relay).running {
		t.Fatal("the browser was stopped while a client was still connected")
	}

	client.Close()
	deadline := time.Now().Add(5 * time.Second)
	for browserOf(t, relay).running && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if browserOf(t, relay).running {
		t.Fatal("the browser stayed up after the last client left")
	}
}

type relayHealth struct {
	running bool
	clients int
}

// browserOf reads the relay's own health endpoint, which reports the browser
// without touching it.
func browserOf(t *testing.T, relay string) relayHealth {
	t.Helper()
	response, err := http.Get("http://" + relay + HealthPath)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()

	var payload struct {
		Browser struct {
			Running bool `json:"running"`
			Clients int  `json:"clients"`
		} `json:"browser"`
	}
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	return relayHealth{running: payload.Browser.Running, clients: payload.Browser.Clients}
}
