// LectureDigest.exe: one-click launcher for the Lecture Digest app.
//
// On first run it downloads everything the app needs into
// %LOCALAPPDATA%\LectureDigest (uv, Python 3.12, the Python packages and
// FFmpeg), then opens the window. Later runs just open the window.
package main

import (
	"archive/tar"
	"archive/zip"
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

//go:embed app/*
var appFiles embed.FS

const pythonVersion = "3.12"

var (
	home    string
	windows = runtime.GOOS == "windows"
)

func main() {
	fmt.Println("Lecture Digest")
	fmt.Println("==============")
	if err := run(); err != nil {
		fmt.Println()
		fmt.Println("Something went wrong:")
		fmt.Println("  ", err)
		fmt.Println()
		fmt.Println("Check your internet connection and free disk space, then run LectureDigest again.")
		fmt.Println("If it keeps failing, send this message to Claude.")
		fmt.Print("\nPress Enter to close...")
		bufio.NewReader(os.Stdin).ReadString('\n')
		os.Exit(1)
	}
}

func run() error {
	if windows {
		home = filepath.Join(os.Getenv("LOCALAPPDATA"), "LectureDigest")
	} else {
		h, _ := os.UserHomeDir()
		home = filepath.Join(h, ".local", "share", "LectureDigest")
	}
	if dir := os.Getenv("LECTURE_DIGEST_HOME"); dir != "" {
		home = dir
	}
	appDir := filepath.Join(home, "app")
	toolsDir := filepath.Join(home, "tools")
	venv := filepath.Join(home, "venv")
	fmt.Println("Install folder:", home)

	if err := os.MkdirAll(toolsDir, 0o755); err != nil {
		return err
	}
	if err := writeAppFiles(appDir); err != nil {
		return fmt.Errorf("copying app files: %w", err)
	}

	env := append(os.Environ(),
		"UV_PYTHON_INSTALL_DIR="+filepath.Join(home, "python"),
		"UV_CACHE_DIR="+filepath.Join(home, "cache"),
		"HF_HOME="+filepath.Join(home, "models"), // speech models live here too
		"PYTHONUTF8=1",
	)

	uv, err := ensureUV(toolsDir)
	if err != nil {
		return fmt.Errorf("getting uv: %w", err)
	}
	ffDir, err := ensureFFmpeg(toolsDir)
	if err != nil {
		return fmt.Errorf("getting FFmpeg: %w", err)
	}
	if ffDir != "" {
		env = setPath(env, ffDir)
	}

	py := filepath.Join(venv, "bin", "python")
	pyw := py
	if windows {
		py = filepath.Join(venv, "Scripts", "python.exe")
		pyw = filepath.Join(venv, "Scripts", "pythonw.exe")
	}
	if !exists(py) {
		os.RemoveAll(venv)
		step("Installing Python " + pythonVersion + " (one time)")
		if err := runCmd(env, uv, "venv", "--managed-python", "--python", pythonVersion, venv); err != nil {
			return fmt.Errorf("installing Python: %w", err)
		}
	}

	reqs, _ := os.ReadFile(filepath.Join(appDir, "requirements.txt"))
	sum := sha256.Sum256(reqs)
	stampFile := filepath.Join(venv, ".packages-installed")
	if old, _ := os.ReadFile(stampFile); string(old) != hex.EncodeToString(sum[:]) {
		step("Installing the speech, GPU and OCR packages (one time, about 1.5 GB - this can take a while)")
		if err := runCmd(env, uv, "pip", "install", "--python", py, "-r", filepath.Join(appDir, "requirements.txt")); err != nil {
			return fmt.Errorf("installing packages: %w", err)
		}
		os.WriteFile(stampFile, []byte(hex.EncodeToString(sum[:])), 0o644)
	}

	if out, err := exec.Command("nvidia-smi", "--query-gpu=name", "--format=csv,noheader").Output(); err == nil {
		fmt.Println("NVIDIA GPU:", strings.TrimSpace(string(out)))
	} else {
		fmt.Println("No NVIDIA GPU driver found. The app will work, but slowly. Install the latest NVIDIA driver to use your GPU.")
	}

	step("Opening Lecture Digest")
	logFile, _ := os.Create(filepath.Join(home, "app-errors.log"))
	cmd := exec.Command(pyw, filepath.Join(appDir, "gui.py"))
	cmd.Env = env
	cmd.Dir = appDir
	if logFile != nil {
		cmd.Stdout, cmd.Stderr = logFile, logFile
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("starting the app: %w", err)
	}
	// If the window dies straight away, show why instead of vanishing silently.
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			msg, _ := os.ReadFile(filepath.Join(home, "app-errors.log"))
			return fmt.Errorf("the app closed with an error: %v\n%s", err, tail(string(msg), 1500))
		}
	case <-time.After(6 * time.Second):
		// running fine; this console can close, the window stays open
	}
	return nil
}

func step(msg string) { fmt.Printf("\n>> %s\n", msg) }

func exists(p string) bool { _, err := os.Stat(p); return err == nil }

func tail(s string, n int) string {
	if len(s) > n {
		return "..." + s[len(s)-n:]
	}
	return s
}

func setPath(env []string, dir string) []string {
	for i, kv := range env {
		if k, v, ok := strings.Cut(kv, "="); ok && strings.EqualFold(k, "PATH") {
			env[i] = k + "=" + dir + string(os.PathListSeparator) + v
			return env
		}
	}
	return append(env, "PATH="+dir)
}

func runCmd(env []string, name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Env = env
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	return cmd.Run()
}

func writeAppFiles(dir string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	return fs.WalkDir(appFiles, "app", func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		data, err := appFiles.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(dir, filepath.Base(p)), data, 0o644)
	})
}

// ---------------------------------------------------------------- uv ----

func ensureUV(toolsDir string) (string, error) {
	dir := filepath.Join(toolsDir, "uv")
	exe := filepath.Join(dir, "uv")
	url := "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-unknown-linux-gnu.tar.gz"
	if windows {
		exe += ".exe"
		url = "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
	}
	if exists(exe) {
		return exe, nil
	}
	step("Downloading uv (Python installer, one time)")
	archive, err := download(url, toolsDir)
	if err != nil {
		return "", err
	}
	defer os.Remove(archive)
	if err := extractNamed(archive, dir, []string{filepath.Base(exe)}); err != nil {
		return "", err
	}
	if !exists(exe) {
		return "", fmt.Errorf("uv was not found in the download")
	}
	return exe, nil
}

// ------------------------------------------------------------ FFmpeg ----

// ensureFFmpeg returns a folder to put on PATH, or "" if FFmpeg is already installed.
func ensureFFmpeg(toolsDir string) (string, error) {
	dir := filepath.Join(toolsDir, "ffmpeg")
	ext := ""
	if windows {
		ext = ".exe"
	}
	if exists(filepath.Join(dir, "ffmpeg"+ext)) && exists(filepath.Join(dir, "ffprobe"+ext)) {
		return dir, nil
	}
	if _, err1 := exec.LookPath("ffmpeg"); err1 == nil {
		if _, err2 := exec.LookPath("ffprobe"); err2 == nil {
			return "", nil
		}
	}
	if !windows {
		return "", fmt.Errorf("please install FFmpeg with your package manager (e.g. sudo apt install ffmpeg)")
	}
	step("Downloading FFmpeg (video tools, one time, about 200 MB)")
	var lastErr error
	for _, url := range []string{
		"https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
		"https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
	} {
		archive, err := download(url, toolsDir)
		if err != nil {
			lastErr = err
			continue
		}
		err = extractNamed(archive, dir, []string{"ffmpeg.exe", "ffprobe.exe"})
		os.Remove(archive)
		if err == nil && exists(filepath.Join(dir, "ffmpeg.exe")) && exists(filepath.Join(dir, "ffprobe.exe")) {
			return dir, nil
		}
		lastErr = fmt.Errorf("ffmpeg.exe not found in %s (%v)", url, err)
	}
	return "", lastErr
}

// ---------------------------------------------------------- helpers ----

type progress struct {
	total, done int64
	last        time.Time
}

func (p *progress) Write(b []byte) (int, error) {
	p.done += int64(len(b))
	if time.Since(p.last) > 700*time.Millisecond {
		p.last = time.Now()
		if p.total > 0 {
			fmt.Printf("\r   %d / %d MB (%d%%)   ", p.done>>20, p.total>>20, p.done*100/p.total)
		} else {
			fmt.Printf("\r   %d MB   ", p.done>>20)
		}
	}
	return len(b), nil
}

func download(url, dir string) (string, error) {
	resp, err := http.Get(url)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("download failed (%s): %s", resp.Status, url)
	}
	name := filepath.Base(resp.Request.URL.Path)
	if !strings.HasSuffix(name, ".zip") && !strings.HasSuffix(name, ".tar.gz") {
		name = filepath.Base(url)
	}
	f, err := os.CreateTemp(dir, "dl-*-"+name)
	if err != nil {
		return "", err
	}
	defer f.Close()
	p := &progress{total: resp.ContentLength}
	if _, err := io.Copy(io.MultiWriter(f, p), resp.Body); err != nil {
		os.Remove(f.Name())
		return "", err
	}
	fmt.Printf("\r   done (%d MB)              \n", p.done>>20)
	return f.Name(), nil
}

// extractNamed copies only the files with the given base names out of a
// .zip or .tar.gz archive into dir, wherever they sit inside it.
func extractNamed(archive, dir string, names []string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	want := map[string]bool{}
	for _, n := range names {
		want[strings.ToLower(n)] = true
	}
	save := func(name string, r io.Reader) error {
		f, err := os.OpenFile(filepath.Join(dir, name), os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
		if err != nil {
			return err
		}
		defer f.Close()
		_, err = io.Copy(f, r)
		return err
	}
	if strings.HasSuffix(archive, ".zip") {
		zr, err := zip.OpenReader(archive)
		if err != nil {
			return err
		}
		defer zr.Close()
		for _, zf := range zr.File {
			base := filepath.Base(zf.Name)
			if zf.FileInfo().IsDir() || !want[strings.ToLower(base)] {
				continue
			}
			rc, err := zf.Open()
			if err != nil {
				return err
			}
			err = save(base, rc)
			rc.Close()
			if err != nil {
				return err
			}
		}
		return nil
	}
	f, err := os.Open(archive)
	if err != nil {
		return err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return err
	}
	tr := tar.NewReader(gz)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}
		base := filepath.Base(h.Name)
		if h.Typeflag == tar.TypeReg && want[strings.ToLower(base)] {
			if err := save(base, tr); err != nil {
				return err
			}
		}
	}
}
