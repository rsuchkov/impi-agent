// Command relay exposes Chrome's DevTools endpoint outside the container and
// runs the browser only while something is attached.
//
// Chrome deliberately binds the debugging port to loopback and ignores
// --remote-debugging-address, so a published container port reaches nothing on
// its own. This relay listens on a routable address, forwards to Chrome on
// loopback, and rewrites the authority inside /json/* responses.
//
// It also owns Chrome's lifecycle: the browser starts on the first client
// connection and stops once the last one has been gone for -idle-timeout. An
// idle container therefore costs this process alone rather than Chrome's
// several hundred megabytes.
//
// Usage:
//
//	relay -listen 0.0.0.0:9222 -upstream 127.0.0.1:9223 -- google-chrome --flags...
//	relay -healthcheck
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	listen := flag.String("listen", "0.0.0.0:9222", "address to accept CDP clients on")
	upstream := flag.String("upstream", "127.0.0.1:9223", "address Chrome listens on")
	idleTimeout := flag.Duration("idle-timeout", 5*time.Minute, "stop the browser after this long with no clients")
	startTimeout := flag.Duration("start-timeout", 30*time.Second, "how long the browser gets to open its port")
	healthcheck := flag.Bool("healthcheck", false, "probe a running relay and exit; does not start the browser")
	flag.Parse()

	log.SetFlags(log.LstdFlags | log.LUTC)

	if *healthcheck {
		if err := probe(*listen); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}

	command := flag.Args()
	if len(command) == 0 {
		fmt.Fprintln(os.Stderr, "no browser command given (expected: ... -- google-chrome --flags)")
		os.Exit(2)
	}

	if err := run(*listen, *upstream, command, *idleTimeout, *startTimeout); err != nil {
		log.Fatal(err)
	}
}

func run(listen, upstream string, command []string, idle, start time.Duration) error {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	browser := NewBrowser(command, upstream, idle, start)
	defer browser.Stop("shutting down")

	listener, err := net.Listen("tcp", listen)
	if err != nil {
		return fmt.Errorf("listening on %s: %w", listen, err)
	}

	log.Printf("relaying %s -> %s, browser starts on demand, idle timeout %s", listen, upstream, idle)
	return NewProxy(browser, upstream).Serve(ctx, listener)
}

// probe asks the relay how it is doing without waking the browser.
func probe(listen string) error {
	// The listen address may be a wildcard, which is not dialable everywhere.
	_, port, err := net.SplitHostPort(listen)
	if err != nil {
		return fmt.Errorf("bad listen address %q: %w", listen, err)
	}

	client := http.Client{Timeout: 3 * time.Second}
	response, err := client.Get("http://127.0.0.1:" + port + HealthPath)
	if err != nil {
		return fmt.Errorf("relay not answering: %w", err)
	}
	defer response.Body.Close()

	body, _ := io.ReadAll(response.Body)
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("relay unhealthy: %s", response.Status)
	}
	fmt.Printf("%s", body)
	return nil
}
