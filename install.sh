#!/usr/bin/env sh
set -eu

source_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
install_home=${HOME:?HOME must be set}
share_parent="$install_home/.local/share"
share_dir="$share_parent/cw"
bin_dir="$install_home/.local/bin"

mkdir -p "$share_parent" "$bin_dir"
stage=$(mktemp -d "$share_parent/.cw-install.XXXXXX")
backup="$share_parent/.cw-previous"
cleanup() {
  if [ -d "$stage" ]; then
    rm -rf -- "$stage"
  fi
}
trap cleanup EXIT HUP INT TERM

cp -R "$source_root/cw" "$stage/cw"
find "$stage" -type d -name __pycache__ -prune -exec rm -rf -- {} +
cp "$source_root/VERSION" "$stage/VERSION"
cp "$source_root/LICENSE" "$stage/LICENSE"
cp "$source_root/NOTICE" "$stage/NOTICE"
printf '%s\n' \
  '#!/usr/bin/env python3' \
  'from cw.cli.main import main' \
  'raise SystemExit(main())' > "$stage/entrypoint.py"
chmod 0755 "$stage/entrypoint.py"

if [ -d "$backup" ]; then
  rm -rf -- "$backup"
fi
if [ -d "$share_dir" ]; then
  mv "$share_dir" "$backup"
fi
if ! mv "$stage" "$share_dir"; then
  if [ -d "$backup" ]; then
    mv "$backup" "$share_dir"
  fi
  exit 1
fi
if [ -d "$backup" ]; then
  rm -rf -- "$backup"
fi

launcher="$bin_dir/.cw.new"
printf '%s\n' \
  '#!/usr/bin/env sh' \
  'set -eu' \
  'cw_share="$HOME/.local/share/cw"' \
  'exec python3 "$cw_share/entrypoint.py" "$@"' > "$launcher"
chmod 0755 "$launcher"
mv "$launcher" "$bin_dir/cw"

path_line='export PATH="$HOME/.local/bin:$PATH"'
for rc in "$install_home/.profile" "$install_home/.zshrc"; do
  touch "$rc"
  if ! grep -Fqx "$path_line" "$rc"; then
    printf '\n%s\n' "$path_line" >> "$rc"
  fi
done

printf 'Installed CW by Queopius %s\n' "$(tr -d '\n' < "$source_root/VERSION")"
printf 'Executable: %s\n' "$bin_dir/cw"
case ":${PATH:-}:" in
  *":$bin_dir:"*) ;;
  *) printf 'Restart your shell or run: export PATH="$HOME/.local/bin:$PATH"\n' ;;
esac
