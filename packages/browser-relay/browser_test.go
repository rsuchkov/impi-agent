package main

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// listenerCommand returns a shell command that opens `address` and holds it,
// standing in for Chrome opening its debugging port.
func listenerCommand(address string) []string {
	_, port, _ := net.SplitHostPort(address)
	return []string{
		"/bin/sh", "-c",
		"exec python3 -c \"import socket,time;s=socket.socket();" +
			"s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);" +
			"s.bind(('127.0.0.1'," + port + "));s.listen(8);time.sleep(300)\"",
	}
}

func freePort(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	return address
}

func TestBrowserStartsOnAcquireAndStopsWhenIdle(t *testing.T) {
	address := freePort(t)
	browser := NewBrowser(listenerCommand(address), address, 150*time.Millisecond, 10*time.Second)
	defer browser.Stop("test over")

	if browser.Running() {
		t.Fatal("browser should not run before the first client")
	}

	if err := browser.Acquire(context.Background()); err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	if !browser.Running() {
		t.Fatal("Acquire did not start the browser")
	}

	browser.Release()

	// Still up immediately after release; the idle timer has not fired yet.
	if !browser.Running() {
		t.Fatal("browser stopped before the idle timeout elapsed")
	}

	deadline := time.Now().Add(5 * time.Second)
	for browser.Running() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if browser.Running() {
		t.Fatal("browser still running after the idle timeout")
	}
}

func TestSecondClientKeepsTheBrowserAlive(t *testing.T) {
	address := freePort(t)
	browser := NewBrowser(listenerCommand(address), address, 150*time.Millisecond, 10*time.Second)
	defer browser.Stop("test over")

	ctx := context.Background()
	if err := browser.Acquire(ctx); err != nil {
		t.Fatalf("first Acquire: %v", err)
	}
	if err := browser.Acquire(ctx); err != nil {
		t.Fatalf("second Acquire: %v", err)
	}

	// One client leaving must not take the browser down under the other.
	browser.Release()
	time.Sleep(400 * time.Millisecond)
	if !browser.Running() {
		t.Fatal("browser stopped while a client was still attached")
	}
	if got := browser.Clients(); got != 1 {
		t.Errorf("Clients() = %d, want 1", got)
	}

	browser.Release()
	deadline := time.Now().Add(5 * time.Second)
	for browser.Running() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if browser.Running() {
		t.Fatal("browser still running after the last client left")
	}
}

func TestReacquireRestartsAfterIdleStop(t *testing.T) {
	address := freePort(t)
	browser := NewBrowser(listenerCommand(address), address, 100*time.Millisecond, 10*time.Second)
	defer browser.Stop("test over")

	ctx := context.Background()
	if err := browser.Acquire(ctx); err != nil {
		t.Fatalf("Acquire: %v", err)
	}
	browser.Release()

	deadline := time.Now().Add(5 * time.Second)
	for browser.Running() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if browser.Running() {
		t.Fatal("browser did not stop")
	}

	// The port must be free again, or the restart would fail — this is what
	// catches a stop that leaves the process group behind.
	if err := browser.Acquire(ctx); err != nil {
		t.Fatalf("re-Acquire after idle stop: %v", err)
	}
	if !browser.Running() {
		t.Fatal("browser did not restart")
	}
	browser.Release()
}

func TestAcquireFailsWhenTheBrowserNeverOpensItsPort(t *testing.T) {
	address := freePort(t)
	browser := NewBrowser([]string{"/bin/sh", "-c", "sleep 300"}, address, time.Minute, 300*time.Millisecond)
	defer browser.Stop("test over")

	if err := browser.Acquire(context.Background()); err == nil {
		t.Fatal("expected Acquire to fail when the port never opens")
	}
	if got := browser.Clients(); got != 0 {
		t.Errorf("Clients() = %d after a failed Acquire, want 0", got)
	}
}

func TestClearsStaleSingletonLockFromAnotherContainer(t *testing.T) {
	// A profile volume outlives the container. Chrome records "hostname-pid" in
	// SingletonLock and refuses to start when the hostname is not its own, so
	// without this every restart after a hard stop would fail.
	dir := t.TempDir()
	lock := filepath.Join(dir, "SingletonLock")
	if err := os.Symlink("6528f1b12064-19", lock); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"SingletonCookie", "SingletonSocket"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("stale"), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	clearStaleSingleton(userDataDirFrom([]string{"google-chrome", "--user-data-dir=" + dir}))

	for _, name := range singletonFiles {
		if _, err := os.Lstat(filepath.Join(dir, name)); !os.IsNotExist(err) {
			t.Errorf("%s survived, Chrome would refuse to start", name)
		}
	}
}

func TestUserDataDirFrom(t *testing.T) {
	got := userDataDirFrom([]string{"google-chrome", "--headless", "--user-data-dir=/profile/user-data"})
	if got != "/profile/user-data" {
		t.Errorf("got %q, want /profile/user-data", got)
	}
	if got := userDataDirFrom([]string{"google-chrome", "--headless"}); got != "" {
		t.Errorf("got %q, want empty when the flag is absent", got)
	}
}

func TestAFailedStartIsNotRememberedAsRunning(t *testing.T) {
	// The contract, not the race: after a start that failed, nothing is
	// running and the next client gets a real second attempt rather than a
	// "browser is not reachable" for a process that died.
	//
	// The window this guards is narrow enough that the test cannot force it —
	// the reaper goroutine clears the recorded process a moment after the
	// failed launch stops waiting on it, and in practice wins. The assertion
	// is what the caller is entitled to see, which is worth stating whether or
	// not a scheduler can be made to lose that race.
	address := freePort(t)
	browser := NewBrowser([]string{"/bin/sh", "-c", "sleep 300"}, address, time.Minute, 200*time.Millisecond)
	defer browser.Stop("test over")

	if err := browser.Acquire(context.Background()); err == nil {
		t.Fatal("expected Acquire to fail when the port never opens")
	}
	if browser.Running() {
		t.Fatal("a browser that failed to start is still recorded as running")
	}

	// And the retry actually launches, rather than being skipped.
	browser.command = listenerCommand(address)
	if err := browser.Acquire(context.Background()); err != nil {
		t.Fatalf("second Acquire: %v", err)
	}
	if !browser.Running() {
		t.Fatal("the retry did not start the browser")
	}
}
