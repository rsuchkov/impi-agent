package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// HealthPath is answered by the relay itself. It must never touch the browser:
// a health check that reached /json/version would start Chrome on every probe
// and defeat the whole point of starting it lazily.
const HealthPath = "/relay/health"

type Proxy struct {
	browser  *Browser
	upstream string
}

func NewProxy(browser *Browser, upstream string) *Proxy {
	return &Proxy{browser: browser, upstream: upstream}
}

func (p *Proxy) Serve(ctx context.Context, listener net.Listener) error {
	var wg sync.WaitGroup
	defer wg.Wait()

	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()

	for {
		conn, err := listener.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return nil
			default:
			}
			return err
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			p.handle(ctx, conn)
		}()
	}
}

func (p *Proxy) handle(ctx context.Context, client net.Conn) {
	defer client.Close()

	// Clients keep the connection alive and send the WebSocket upgrade on the
	// same socket they just used for /json/version. Closing after one response
	// makes that upgrade land on a dead socket, which surfaces client-side as an
	// unexplained "socket hang up".
	reader := bufio.NewReader(client)
	holding := false
	defer func() {
		if holding {
			p.browser.Release()
		}
	}()

	for {
		request, err := http.ReadRequest(reader)
		if err != nil {
			if err != io.EOF && !isClosedConn(err) {
				log.Printf("read request from %s: %v", client.RemoteAddr(), err)
			}
			return
		}

		if request.URL.Path == HealthPath {
			p.writeHealth(client)
			continue
		}

		if !holding {
			if err := p.browser.Acquire(ctx); err != nil {
				log.Printf("cannot start browser: %v", err)
				writeError(client, http.StatusBadGateway, err.Error())
				return
			}
			holding = true
		}

		upstream, err := net.DialTimeout("tcp", p.upstream, 5*time.Second)
		if err != nil {
			log.Printf("dial upstream: %v", err)
			writeError(client, http.StatusBadGateway, "browser is not reachable")
			return
		}

		tunnelled, err := p.forward(request, reader, client, upstream)
		upstream.Close()
		if err != nil && !isClosedConn(err) {
			log.Printf("forward %s: %v", request.URL.Path, err)
		}
		if tunnelled || err != nil {
			return
		}
	}
}

// forward relays one request. It reports whether the connection turned into an
// opaque tunnel, after which no further requests can be read from it.
func (p *Proxy) forward(request *http.Request, reader *bufio.Reader, client, upstream net.Conn) (bool, error) {
	// Captured before the Host is rewritten: this is the authority the client
	// addressed, and what the rewritten webSocketDebuggerUrl must point back at.
	clientAuthority := request.Host

	// Chrome rejects DevTools requests whose Host is neither localhost nor an
	// IP, so upstream always gets the loopback authority.
	request.Host = p.upstream
	// Chrome 111+ refuses a DevTools WebSocket carrying an Origin unless started
	// with --remote-allow-origins. Dropping the header keeps that hardening on
	// for everything else.
	request.Header.Del("Origin")

	if err := request.Write(upstream); err != nil {
		return false, fmt.Errorf("writing request upstream: %w", err)
	}

	if isWebSocketUpgrade(request) {
		// The 101 and everything after it is relayed as raw bytes: parsing an
		// upgrade response with net/http would consume part of the stream.
		return true, tunnel(client, reader, upstream)
	}

	response, err := http.ReadResponse(bufio.NewReader(upstream), request)
	if err != nil {
		return false, fmt.Errorf("reading response: %w", err)
	}
	defer response.Body.Close()

	body, err := io.ReadAll(response.Body)
	if err != nil {
		return false, fmt.Errorf("reading response body: %w", err)
	}

	// Chrome advertises its own loopback address in webSocketDebuggerUrl. Left
	// alone, a client would follow ws://127.0.0.1:<internal> back to its own
	// machine instead of into the container.
	if clientAuthority != "" {
		body = bytes.ReplaceAll(body, []byte(p.upstream), []byte(clientAuthority))
	}

	response.Body = io.NopCloser(bytes.NewReader(body))
	response.ContentLength = int64(len(body))
	response.Header.Set("Content-Length", fmt.Sprint(len(body)))
	return false, response.Write(client)
}

// tunnel copies in both directions until either side finishes.
//
// Neither side is half-closed on the way out: a half-closed CDP socket makes
// Chrome drop the session, which the client reports as an unexplained hang-up.
func tunnel(client net.Conn, buffered *bufio.Reader, upstream net.Conn) error {
	done := make(chan error, 2)
	go func() {
		// buffered, not client: the reader may already hold bytes that arrived
		// alongside the upgrade request.
		_, err := io.Copy(upstream, buffered)
		done <- err
	}()
	go func() {
		_, err := io.Copy(client, upstream)
		done <- err
	}()

	err := <-done
	// Closing both ends unblocks the second copy, whose error is then moot.
	client.Close()
	upstream.Close()
	<-done

	if isClosedConn(err) {
		return nil
	}
	return err
}

func (p *Proxy) writeHealth(client net.Conn) {
	payload, _ := json.Marshal(map[string]any{
		"relay":   "ok",
		"browser": map[string]any{"running": p.browser.Running(), "clients": p.browser.Clients()},
	})
	response := http.Response{
		StatusCode:    http.StatusOK,
		ProtoMajor:    1,
		ProtoMinor:    1,
		Header:        http.Header{"Content-Type": []string{"application/json"}},
		Body:          io.NopCloser(bytes.NewReader(payload)),
		ContentLength: int64(len(payload)),
	}
	_ = response.Write(client)
}

func writeError(client net.Conn, status int, message string) {
	body := []byte(message + "\n")
	response := http.Response{
		StatusCode:    status,
		ProtoMajor:    1,
		ProtoMinor:    1,
		Header:        http.Header{"Content-Type": []string{"text/plain"}},
		Body:          io.NopCloser(bytes.NewReader(body)),
		ContentLength: int64(len(body)),
		Close:         true,
	}
	_ = response.Write(client)
}

func isWebSocketUpgrade(request *http.Request) bool {
	return strings.EqualFold(request.Header.Get("Upgrade"), "websocket")
}

func isClosedConn(err error) bool {
	if err == nil {
		return false
	}
	message := err.Error()
	return strings.Contains(message, "use of closed network connection") ||
		strings.Contains(message, "connection reset by peer") ||
		strings.Contains(message, "broken pipe")
}
