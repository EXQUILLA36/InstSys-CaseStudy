import os

class DataLoader:
    def __init__(self, folder_dir, silent=False):
        self.folder_dir = folder_dir
        self.silent = silent
        self.retrieve = False
        self.exclusions = {
            "admin": [],
            "non_faculty": ["admin", "teaching_faculty"],
            "teaching_faculty": ["admin", "non_faculty"]
            # "guest": ["admin", "non_faculty", "teaching_faculty"]
        }

    def log(self, message):
        if not self.silent:
                print(message)
                
    def access_folder(self, assign):
        return [access.lower() for access in assign]

    def load_data(self, role="guest", assign=None):
        uploads_dir = self.folder_dir
        folder_paths = []
        guest_loaded = False

        for folder in os.listdir(uploads_dir):
            if folder.lower() in self.exclusions.get(role.lower(), []):
                continue

            folder_dir = os.path.join(uploads_dir, folder)
            if not os.path.isdir(folder_dir):
                continue

            if folder.lower() == "guest":
                if not guest_loaded:
                    folder_paths.append(folder_dir)
                    guest_loaded = True
                continue

            for root, dirs, files in os.walk(folder_dir):
                if "chroma.sqlite3" in files:
                    if assign:
                        if any(a.lower() in root.lower() for a in self.access_folder(assign)):
                            folder_paths.append(root)
                    else:
                        folder_paths.append(root)
        return folder_paths
                    