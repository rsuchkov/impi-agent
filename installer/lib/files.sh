# File installation helpers. Sourced by the installer and by the `impi` wrapper.

# install_executable SRC DEST — put SRC at DEST by RENAME, never by writing into
# the file that is already there.
#
# The deliberate opposite of env_set (see envfile.sh), which truncates in place
# so the inode survives for a container that has the file mounted. Here the inode
# must NOT survive: DEST may be the very script that is running. bash reads a
# script lazily, by byte offset, so overwriting it mid-run makes the shell resume
# at its old offset inside new content — the classic "syntax error near
# unexpected token" printed after an otherwise successful update. A rename swaps
# the directory entry and leaves the running shell reading its own inode.
install_executable() {
    local src=$1 dest=$2 tmp
    tmp="$(dirname "$dest")/.$(basename "$dest").new.$$"
    cp "$src" "$tmp"
    chmod +x "$tmp"
    mv -f "$tmp" "$dest"
}
