import requests
import os
import zipfile
import shutil
import re
import subprocess
import sys
from datetime import datetime
import time
import signal


class CDNLoader:
    def __init__(self, repo_owner="oblongboot", repo_name="siteadmin", install_dir="siteadmin"):
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases/latest"
        self.install_dir = install_dir
        self.current_path = os.getcwd()
        self.process = None

    def get_newest_cdn(self):
        if not os.path.exists(self.current_path):
            return None

        files = os.listdir(self.current_path)
        cdn_pattern = re.compile(r'^cdn-(\d+\.\d+\.\d+)$')

        cdn_dirs = []
        for file in files:
            match = cdn_pattern.match(file)
            if match and os.path.isdir(os.path.join(self.current_path, file)):
                version = match.group(1)
                version_num = int(version.replace('.', ''))
                cdn_dirs.append({
                    'path': os.path.join(self.current_path, file),
                    'version': version,
                    'version_num': version_num
                })

        if not cdn_dirs:
            return None

        cdn_dirs.sort(key=lambda x: x['version_num'], reverse=True)
        newest = cdn_dirs[0]

        for old_cdn in cdn_dirs[1:]:
            try:
                shutil.rmtree(old_cdn['path'])
                print(f"Cleaned up old version: {old_cdn['version']}")
            except Exception as e:
                print(f"Warning: Could not remove {old_cdn['path']}: {e}")

        return newest

    def backup_persistent_files(self, cdn_path):
        backup_content = {}
        backup_dir = os.path.join(self.current_path, "cdn_backup_temp")
        os.makedirs(backup_dir, exist_ok=True)

        env_file = os.path.join(cdn_path, '.env')
        if os.path.exists(env_file):
            try:
                with open(env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            backup_content[key.strip()] = value.strip()
            except Exception as e:
                print(f"Warning: Could not backup .env file: {e}")

        db_file = os.path.join(cdn_path, 'database.db')
        if os.path.exists(db_file):
            shutil.copy2(db_file, backup_dir)

        uploads_dir = os.path.join(cdn_path, 'uploads')
        if os.path.exists(uploads_dir):
            shutil.copytree(uploads_dir, os.path.join(backup_dir, 'uploads'), dirs_exist_ok=True)

        return backup_content, backup_dir

    def restore_persistent_files(self, cdn_path, env_backup, backup_dir):
        if env_backup:
            self.restore_env_file(cdn_path, env_backup)

        db_backup = os.path.join(backup_dir, 'database.db')
        if os.path.exists(db_backup):
            shutil.copy2(db_backup, os.path.join(cdn_path, 'database.db'))

        uploads_backup = os.path.join(backup_dir, 'uploads')
        if os.path.exists(uploads_backup):
            shutil.copytree(uploads_backup, os.path.join(cdn_path, 'uploads'), dirs_exist_ok=True)

        shutil.rmtree(backup_dir, ignore_errors=True)

    def restore_env_file(self, cdn_path, backup_content):
        if not backup_content:
            return

        env_file = os.path.join(cdn_path, '.env')
        existing_content = {}
        if os.path.exists(env_file):
            try:
                with open(env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            existing_content[key.strip()] = value.strip()
            except Exception:
                pass

        merged_content = {**existing_content, **backup_content}
        try:
            with open(env_file, 'w') as f:
                f.write(f"# Last updated: {datetime.now().isoformat()}\n\n")
                f.write("# should have been migrated from prev version\n")
                for key, value in merged_content.items():
                    f.write(f"{key}={value}\n")
        except Exception as e:
            print(f"Error restoring .env file: {e}")

    def get_latest_release(self):
        try:
            response = requests.get(self.api_url)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Failed to check for auto update: {e}")
            return None

    def download_progress(self, url, output_path):
        try:
            response = requests.get(url, stream=True)
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            print("Downloading CDN update...")

            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            progress = (downloaded / total_size) * 100
                            bar_length = 40
                            filled = int(bar_length * downloaded / total_size)
                            bar = '█' * filled + '-' * (bar_length - filled)
                            print(f'\r[{bar}] {progress:.1f}% ({downloaded}/{total_size} bytes)', end='')

            print()
            return True
        except Exception as e:
            print(f"Error downloading: {e}")
            return False

    def download_and_extract(self, release, version):
        zip_asset = None
        for asset in release['assets']:
            if asset['name'].endswith('.zip'):
                zip_asset = asset
                break

        if not zip_asset:
            print("No zip file found in release")
            return None

        temp_zip = os.path.join(self.current_path, f"cdn-{version}-temp.zip")
        if not self.download_progress(zip_asset['browser_download_url'], temp_zip):
            return None

        extract_dir = os.path.join(self.current_path, f"cdn-{version}")
        try:
            with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
            os.remove(temp_zip)
            return extract_dir
        except Exception as e:
            print(f"Error extracting: {e}")
            if os.path.exists(temp_zip):
                os.remove(temp_zip)
            return None

    def stop_cdn(self):
        """Stop the currently running CDN process"""
        if self.process and self.process.poll() is None:
            print("Stopping current CDN instance...")
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
                print("CDN stopped successfully")
            except subprocess.TimeoutExpired:
                print("CDN didn't stop gracefully, forcing...")
                self.process.kill()
                self.process.wait()
            except Exception as e:
                print(f"Error stopping CDN: {e}")
            finally:
                self.process = None

    def launch_cdn(self, cdn_path):
        for entry in ['main.py']:
            entry_path = os.path.join(cdn_path, entry)
            if os.path.exists(entry_path):
                print(f"\nLaunching CDN from {entry_path}...")
                try:
                    self.process = subprocess.Popen(
                        [sys.executable, entry_path],
                        cwd=cdn_path
                    )
                    print(f"CDN launched successfully (PID: {self.process.pid})")
                    return self.process
                except Exception as e:
                    print(f"Error launching CDN: {e}")
                    return None

        print(f"Warning: No entry point found in {cdn_path}")
        return None

    def check_and_update(self):
        """Check for updates and update if needed"""
        print("Checking for CDN updates...")
        latest_release = self.get_latest_release()
        newest = self.get_newest_cdn()

        env_backup = {}
        backup_dir = None
        if newest:
            env_backup, backup_dir = self.backup_persistent_files(newest['path'])

        if not latest_release:
            print("Failed to check for updates.")
            if newest and not self.process:
                self.launch_cdn(newest['path'])
            return

        latest_version = latest_release['tag_name']
        latest_version_num = int(latest_version.replace('.', ''))

        if newest and newest['version_num'] == latest_version_num:
            print(f"CDN up to date (version {newest['version']})")
            if not self.process or self.process.poll() is not None:
                print("CDN not running, launching...")
                self.launch_cdn(newest['path'])
            return

        print(f"New CDN version available: {latest_version}")
        
        self.stop_cdn()
        
        new_cdn_path = self.download_and_extract(latest_release, latest_version)
        if not new_cdn_path:
            print("Download failed. Launching existing version if available...")
            if newest:
                self.launch_cdn(newest['path'])
            return

        if newest:
            self.restore_persistent_files(new_cdn_path, env_backup, backup_dir)
            try:
                shutil.rmtree(newest['path'])
                print(f"Removed old version: {newest['version']}")
            except Exception as e:
                print(f"Warning: Could not remove old version: {e}")

        print(f"CDN updated to {latest_version}!")
        self.launch_cdn(new_cdn_path)

    def run_with_updates(self, check_interval=3*60*60):
        """Run CDN with periodic update checks"""
        last_check = 0
        self.check_and_update()
        flag_file = os.path.join(self.current_path, "UPDATE_REQUESTED")
        
        try:
            while True:
                now = time.time()
                
                if os.path.exists(flag_file):
                    print("Update flag detected from webhook, checking for updates...")
                    try:
                        os.remove(flag_file)
                    except:
                        pass
                    self.check_and_update()
                    last_check = now
                
                elif now - last_check >= check_interval:
                    self.check_and_update()
                    last_check = now
                
                if self.process and self.process.poll() is not None:
                    print("CDN process died unexpectedly, restarting...")
                    newest = self.get_newest_cdn()
                    if newest:
                        self.launch_cdn(newest['path'])
                
                time.sleep(10)
        except KeyboardInterrupt:
            print("\nShutting down...")
            self.stop_cdn()
            try:
                if os.path.exists(flag_file):
                    os.remove(flag_file)
            except:
                pass


def main():
    loader = CDNLoader()
    loader.run_with_updates(check_interval=3*60*60)


if __name__ == "__main__":
    main()
