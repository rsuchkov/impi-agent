package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Browser starts Chrome on demand and stops it once nothing is connected.
//
// Chrome is by far the largest thing in the container — roughly 370 MB against
// this process's few — so an idle container costs almost nothing while no
// client is attached. The profile lives on a volume, so cookies and any
// signed-in session survive a stop/start cycle.
type Browser struct {
	command      []string
	upstream     string
	idleTimeout  time.Duration
	startTimeout time.Duration

	// opMu serialises whole start/stop operations against each other, so a
	// concurrent Acquire cannot race an idle Stop halfway through killing
	// Chrome. It is never held by the health endpoint.
	opMu sync.Mutex

	// mu guards only the fields below, and is held for very short stretches so
	// status reads never block behind a 30-second browser start.
	mu        sync.Mutex
	cmd       *exec.Cmd
	exited    chan struct{}
	clients   int
	idleTimer *time.Timer
}

func NewBrowser(command []string, upstream string, idle, start time.Duration) *Browser {
	return &Browser{
		command:      command,
		upstream:     upstream,
		idleTimeout:  idle,
		startTimeout: start,
	}
}

// Acquire makes sure Chrome is up and registers a client against it.
// Every successful call must be paired with Release.
func (b *Browser) Acquire(ctx context.Context) error {
	b.opMu.Lock()
	defer b.opMu.Unlock()

	b.mu.Lock()
	if b.idleTimer != nil {
		b.idleTimer.Stop()
		b.idleTimer = nil
	}
	running := b.cmd != nil
	b.mu.Unlock()

	if !running {
		if err := b.launch(ctx); err != nil {
			return err
		}
	}

	b.mu.Lock()
	b.clients++
	b.mu.Unlock()
	return nil
}

// Release drops a client and arms the idle timer once the last one is gone.
func (b *Browser) Release() {
	b.mu.Lock()
	defer b.mu.Unlock()

	if b.clients == 0 {
		log.Printf("relay: Release without Acquire, ignoring")
		return
	}
	b.clients--
	if b.clients > 0 || b.cmd == nil {
		return
	}
	b.idleTimer = time.AfterFunc(b.idleTimeout, func() { b.Stop("idle") })
}

// singletonFiles are the profile locks Chrome uses to refuse a second instance.
var singletonFiles = []string{"SingletonLock", "SingletonCookie", "SingletonSocket"}

// clearStaleSingleton removes a lock left behind by a Chrome that did not exit
// cleanly.
//
// The lock records "hostname-pid", and Chrome refuses to start when the
// hostname is not its own, since it cannot tell whether that process is alive.
// Every container gets a fresh hostname, so with a persistent profile volume
// any hard stop — SIGKILL after the shutdown grace period, `docker kill`, an
// OOM — would make every later start fail with "profile appears to be in use".
//
// Safe here because the caller holds opMu and has established that no browser
// of ours is running, and nothing else shares this profile.
func clearStaleSingleton(userDataDir string) {
	if userDataDir == "" {
		return
	}
	for _, name := range singletonFiles {
		path := filepath.Join(userDataDir, name)
		// Removes the symlink itself rather than following it.
		if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
			log.Printf("could not clear %s: %v", path, err)
		}
	}
}

// userDataDirFrom reads the profile path out of the browser command, so the
// profile location is configured in exactly one place.
func userDataDirFrom(command []string) string {
	const flag = "--user-data-dir="
	for _, arg := range command {
		if strings.HasPrefix(arg, flag) {
			return strings.TrimPrefix(arg, flag)
		}
	}
	return ""
}

func (b *Browser) launch(ctx context.Context) error {
	clearStaleSingleton(userDataDirFrom(b.command))

	cmd := exec.Command(b.command[0], b.command[1:]...)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	// Chrome spawns a tree of helpers; a process group lets all of them be
	// signalled at once instead of leaving orphans behind on every stop.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting browser: %w", err)
	}
	log.Printf("browser started (pid %d)", cmd.Process.Pid)

	exited := make(chan struct{})
	b.mu.Lock()
	b.cmd = cmd
	b.exited = exited
	b.mu.Unlock()

	go func() {
		err := cmd.Wait()
		close(exited)
		b.mu.Lock()
		defer b.mu.Unlock()
		// Only clear it if this is still the current process, or a stop/start
		// race would wipe out the newer one.
		if b.cmd == cmd {
			b.cmd = nil
			log.Printf("browser exited (%v)", err)
		}
	}()

	if err := waitForPort(ctx, b.upstream, b.startTimeout, exited); err != nil {
		b.terminate(cmd, exited, "failed to start")
		// The reaper above clears b.cmd, but only after closing `exited` — which
		// is the very thing terminate waits on, so it may not have run yet. A
		// client retrying in that window would find a dead process recorded as
		// running, skip the launch, and be told the browser is not reachable
		// instead of getting a second attempt at starting it.
		b.mu.Lock()
		if b.cmd == cmd {
			b.cmd = nil
		}
		b.mu.Unlock()
		return err
	}
	return nil
}

// Stop terminates Chrome if it is running. Safe to call when it is not.
func (b *Browser) Stop(reason string) {
	b.opMu.Lock()
	defer b.opMu.Unlock()

	b.mu.Lock()
	cmd, exited := b.cmd, b.exited
	// A client may have connected between the idle timer firing and this lock.
	if cmd == nil || (reason == "idle" && b.clients > 0) {
		b.mu.Unlock()
		return
	}
	b.mu.Unlock()

	b.terminate(cmd, exited, reason)
}

// terminate signals the process group and waits, holding no locks so that
// status reads stay responsive while Chrome shuts down.
func (b *Browser) terminate(cmd *exec.Cmd, exited <-chan struct{}, reason string) {
	if cmd.Process == nil {
		return
	}
	pid := cmd.Process.Pid
	log.Printf("stopping browser (pid %d): %s", pid, reason)

	// A negative pid signals the whole process group.
	_ = syscall.Kill(-pid, syscall.SIGTERM)
	select {
	case <-exited:
	case <-time.After(10 * time.Second):
		log.Printf("browser did not exit on SIGTERM, killing")
		_ = syscall.Kill(-pid, syscall.SIGKILL)
		<-exited
	}
}

// Running reports whether Chrome is up, without starting it.
func (b *Browser) Running() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.cmd != nil
}

// Clients reports how many connections are currently holding the browser open.
func (b *Browser) Clients() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.clients
}

func waitForPort(ctx context.Context, address string, timeout time.Duration, died <-chan struct{}) error {
	deadline := time.Now().Add(timeout)
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-died:
			return errors.New("browser exited during startup")
		default:
		}

		conn, err := net.DialTimeout("tcp", address, 500*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("browser did not open %s within %s", address, timeout)
		}
		time.Sleep(100 * time.Millisecond)
	}
}
