import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import platform
import random
import logging
import shutil
import sys
import threading
import re # For parsing paths with spaces on Windows
import subprocess

# Import the TkinterDnD2 module
# Make sure you have installed it using: pip install tkinterdnd2
from tkinterdnd2 import TkinterDnD, DND_FILES

class FileListApp(TkinterDnD.Tk): # IMPORTANT: Inherit from TkinterDnD.Tk
    def __init__(self):
        super().__init__()

        self.title("File Shredder 1.3")
        self.geometry("700x600") # Slightly larger window
        self.minsize(600, 500) # Set a minimum size

        # Configure logging
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s - %(levelname)s - %(message)s',
                            handlers=[
                                logging.FileHandler("file_shredder.log"), # Log to file
                                logging.StreamHandler(sys.stdout) # Log to console
                            ])
        logging.info("Application started.")

        self.file_list = [] # Stores full paths

        # Cache for SSD drive detection: None means not yet queried
        self._ssd_drives_cache = None

        # Main Frame for better layout management
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Listbox for displaying files/folders
        listbox_frame = ttk.Frame(main_frame)
        listbox_frame.pack(pady=10, padx=10, fill=tk.BOTH, expand=True)

        self.listbox = tk.Listbox(listbox_frame, selectmode=tk.EXTENDED, height=15,
                                  font=('Segoe UI', 10)) # Use EXTENDED for multi-select with shift/ctrl
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(listbox_frame, orient="vertical", command=self.listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.config(yscrollcommand=scrollbar.set)

        # Enable drag-and-drop for the listbox
        self.listbox.drop_target_register(DND_FILES) # Allows dropping files
        self.listbox.dnd_bind('<<Drop>>', self.on_drop) # Bind the drop event
        self.listbox.dnd_bind('<<DragEnter>>', self.on_drag_enter) # Optional: visual feedback
        self.listbox.dnd_bind('<<DragLeave>>', self.on_drag_leave) # Optional: visual feedback

        # Buttons Frame
        buttons_frame = ttk.Frame(main_frame)
        buttons_frame.pack(pady=10, padx=10, fill=tk.X)

        # Using a grid for buttons for better alignment
        buttons_frame.columnconfigure(0, weight=1)
        buttons_frame.columnconfigure(1, weight=1)
        buttons_frame.columnconfigure(2, weight=1)
        buttons_frame.columnconfigure(3, weight=1)

        self.browse_button = ttk.Button(buttons_frame, text="Add Files", command=self.browse_files)
        self.browse_button.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

        self.folder_button = ttk.Button(buttons_frame, text="Add Folders", command=self.select_folders)
        self.folder_button.grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        self.clear_button = ttk.Button(buttons_frame, text="Clear List", command=self.clear_list)
        self.clear_button.grid(row=0, column=2, padx=5, pady=5, sticky="ew")

        self.remove_selected_button = ttk.Button(buttons_frame, text="Remove Selected", command=self.remove_selected_items)
        self.remove_selected_button.grid(row=0, column=3, padx=5, pady=5, sticky="ew")

        self.load_button = ttk.Button(buttons_frame, text="Load List", command=self.load_list)
        self.load_button.grid(row=1, column=0, padx=5, pady=5, sticky="ew")

        self.save_button = ttk.Button(buttons_frame, text="Save List", command=self.save_list)
        self.save_button.grid(row=1, column=1, padx=5, pady=5, sticky="ew")

        self.open_button = ttk.Button(buttons_frame, text="Open Files", command=self.open_files)
        self.open_button.grid(row=1, column=2, padx=5, pady=5, sticky="ew")

        # Shredding Options Frame
        shred_options_frame = ttk.LabelFrame(main_frame, text="Shredding Options", padding="10")
        shred_options_frame.pack(pady=10, padx=10, fill=tk.X)

        self.shred_mode = tk.StringVar(self)
        self.shred_mode.set("Basic Mode") # Default selection

        mode_label = ttk.Label(shred_options_frame, text="Shredding Mode:")
        mode_label.grid(row=0, column=0, padx=5, pady=5, sticky="w")

        self.mode_dropdown = ttk.OptionMenu(shred_options_frame, self.shred_mode, "Basic Mode",
                                            "Basic Mode", # Single pass, overwrite with nulls
                                            "Normal Mode (1 Pass)", # 1 pass, random data
                                            "Normal Mode (3 Pass)", # 3 passes, random data (DoD 5220.22-M like)
                                            "Expert Mode (10 Pass)") # Corrected to 10 passes
        self.mode_dropdown.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        shred_options_frame.columnconfigure(1, weight=1) # Allow dropdown to expand

        self.trim_var = tk.BooleanVar(self, value=True)
        trim_check = ttk.Checkbutton(shred_options_frame, text="Run TRIM on SSD after shredding",
                                     variable=self.trim_var)
        trim_check.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky="w")

        self.shred_button = ttk.Button(main_frame, text="Shred Selected Files/Folders",
                                        command=self.start_shredding_thread, style="Accent.TButton") # Use Accent style for main action
        self.shred_button.pack(pady=10, padx=10, fill=tk.X)
        self.shred_button.config(state=tk.DISABLED) # Initially disabled

        # Status bar
        self.status_bar = ttk.Label(self, text="Ready", relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # Bind listbox selection event to update shred button state
        self.listbox.bind('<<ListboxSelect>>', self.update_shred_button)
        self.listbox.bind('<Delete>', self.remove_selected_items) # Bind Delete key

        # Initial update
        self.update_shred_button()
        logging.info("GUI components initialized.")
        self.update_status("Ready")

    def update_status(self, message):
        """Updates the message in the status bar."""
        self.status_bar.config(text=message)
        self.update_idletasks() # Ensure UI updates immediately

    def on_drag_enter(self, event):
        """Changes listbox background when files are dragged over it."""
        self.listbox.config(bg='lightgray') # Visual feedback
        return event.action

    def on_drag_leave(self, event):
        """Resets listbox background when files are dragged away."""
        self.listbox.config(bg='white') # Reset to default
        return event.action

    def on_drop(self, event):
        """Handles files/folders dropped onto the listbox."""
        self.listbox.config(bg='white') # Reset background
        logging.info(f"Raw dropped data: {event.data}")

        # Use re.findall to robustly parse paths that might contain spaces
        # and are quoted (e.g., "{C:/path with spaces/file.txt}") or unquoted.
        # This regex tries to match quoted strings or non-space strings.
        paths = re.findall(r'\"([^\"]+)\"|\{([^\}]+)\}|(\S+)', event.data)
        parsed_paths = []
        for match in paths:
            # A match will be a tuple like ('path', '', '') or ('', 'path', '') or ('', '', 'path')
            # Get the non-empty string from the tuple.
            p = next((s for s in match if s), None)
            if p:
                parsed_paths.append(p.replace('/', os.sep)) # Convert forward slashes to backslashes

        logging.info(f"Parsed dropped items: {parsed_paths}")

        added_count = 0
        for path in parsed_paths:
            # Canonicalize path to prevent duplicates like 'C:\foo\bar' and 'C:/foo/bar'
            canonical_path = os.path.normpath(path)
            if os.path.exists(canonical_path) and canonical_path not in self.file_list:
                self.file_list.append(canonical_path)
                display_name = os.path.basename(canonical_path)
                if os.path.isdir(canonical_path):
                    display_name += " (Folder)"
                self.listbox.insert(tk.END, display_name)
                added_count += 1
            elif not os.path.exists(canonical_path):
                logging.warning(f"Dropped path does not exist: {canonical_path}")
                self.update_status(f"Skipped non-existent: {os.path.basename(canonical_path)}")
            elif canonical_path in self.file_list:
                logging.info(f"Dropped path already in list: {canonical_path}")

        if added_count > 0:
            self.update_status(f"Added {added_count} new items via drag-and-drop.")
            self.update_shred_button()
        else:
            self.update_status("No new items added via drag-and-drop.")

    def update_shred_button(self, event=None):
        """Enables/disables the shred button based on listbox selection."""
        if self.listbox.curselection():
            self.shred_button.config(state=tk.NORMAL)
            self.remove_selected_button.config(state=tk.NORMAL)
            logging.debug("Shred and Remove Selected buttons enabled.")
        else:
            self.shred_button.config(state=tk.DISABLED)
            self.remove_selected_button.config(state=tk.DISABLED)
            logging.debug("Shred and Remove Selected buttons disabled.")

    def browse_files(self):
        logging.info("Browse files button pressed.")
        file_paths = filedialog.askopenfilenames(
            title="Select Files to Add",
            filetypes=[("All files", "*.*"), ("Text files", "*.txt"), ("Image files", "*.png;*.jpg;*.jpeg;*.gif")]
        )
        if file_paths:
            added_count = 0
            for file_path in file_paths:
                canonical_path = os.path.normpath(file_path)
                if canonical_path not in self.file_list: # Prevent duplicates
                    self.file_list.append(canonical_path)
                    self.listbox.insert(tk.END, os.path.basename(canonical_path))
                    added_count += 1
            if added_count > 0:
                self.update_status(f"Added {added_count} files.")
                self.update_shred_button()
            logging.info(f"Files added: {file_paths}")

    def select_folders(self):
        logging.info("Select folders button pressed.")
        folder_path = filedialog.askdirectory(title="Select Folder to Add")
        if folder_path:
            canonical_path = os.path.normpath(folder_path)
            if canonical_path not in self.file_list: # Prevent duplicates
                self.file_list.append(canonical_path)
                self.listbox.insert(tk.END, os.path.basename(canonical_path) + " (Folder)")
                self.update_status(f"Added folder: {os.path.basename(canonical_path)}")
                self.update_shred_button()
            logging.info(f"Folder added: {folder_path}")

    def clear_list(self):
        logging.info("Clear list button pressed.")
        if messagebox.askyesno("Confirm Clear", "Are you sure you want to clear all items from the list?"):
            self.file_list.clear()
            self.listbox.delete(0, tk.END)
            self.update_shred_button()
            self.update_status("File list cleared.")
            logging.info("File list cleared.")

    def remove_selected_items(self, event=None):
        """Removes selected items from the listbox and internal list."""
        selected_indices = self.listbox.curselection()
        if not selected_indices:
            self.update_status("No items selected to remove.")
            return

        # Create a list of items to remove based on selected indices
        items_to_remove = []
        for i in selected_indices:
            items_to_remove.append(self.file_list[i])

        # Remove from listbox in reverse order to avoid index issues
        for index in sorted(selected_indices, reverse=True):
            self.listbox.delete(index)
            # Remove from the actual file_list. This will be handled by re-building the list below.
            # No need to del self.file_list[index] here directly, as it will be rebuilt.

        # Rebuild file_list to reflect removals
        new_file_list = [self.file_list[i] for i in range(len(self.file_list)) if i not in selected_indices]
        self.file_list = new_file_list

        self.update_status(f"Removed {len(items_to_remove)} selected items.")
        self.update_shred_button()
        logging.info(f"Removed items: {items_to_remove}")


    def open_files(self):
        logging.info("Open files button pressed.")
        selected_indices = self.listbox.curselection()
        if not selected_indices:
            messagebox.showinfo("No Selection", "Please select items from the list to open.")
            return

        opened_count = 0
        for index in selected_indices:
            file_path = self.file_list[index]
            if os.path.isfile(file_path): # Only open actual files
                try:
                    if platform.system() == "Windows":
                        os.startfile(file_path)
                    elif platform.system() == "Darwin": # macOS
                        import subprocess
                        subprocess.run(['open', file_path], check=True)
                    else: # Linux/other Unix-like
                        import subprocess
                        subprocess.run(['xdg-open', file_path], check=True)
                    logging.info(f"Opened file: {file_path}")
                    opened_count += 1
                except Exception as e:
                    logging.error(f"Error opening {file_path}: {e}")
                    messagebox.showerror("Error", f"Could not open {os.path.basename(file_path)}:\n{e}")
            else:
                logging.warning(f"Cannot open '{file_path}': not a file or does not exist.")

        if opened_count > 0:
            self.update_status(f"Attempted to open {opened_count} file(s).")
        else:
            self.update_status("No files were opened.")


    def save_list(self):
        logging.info("Save list button pressed.")
        if not self.file_list:
            messagebox.showinfo("Empty List", "The list is empty. Nothing to save.")
            return

        filepath = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            title="Save File List As"
        )
        if filepath:
            try:
                with open(filepath, 'w') as f:
                    for file_path in self.file_list:
                        f.write(f"{file_path}\n")
                logging.info(f"File list saved to: {filepath}")
                messagebox.showinfo("Success", "File list saved successfully!")
                self.update_status(f"List saved to: {os.path.basename(filepath)}")
            except Exception as e:
                logging.error(f"Error saving list: {e}")
                messagebox.showerror("Error", f"Error saving list:\n{e}")
                self.update_status("Failed to save list.")


    def load_list(self):
        logging.info("Load list button pressed.")
        filepath = filedialog.askopenfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            title="Load File List"
        )
        if filepath:
            try:
                self.file_list.clear()
                self.listbox.delete(0, tk.END)
                loaded_count = 0
                with open(filepath, 'r') as f:
                    for line in f:
                        file_path = os.path.normpath(line.strip()) # Normalize path
                        if os.path.exists(file_path) and file_path not in self.file_list: # Check existence and prevent duplicates
                            self.file_list.append(file_path)
                            self.listbox.insert(tk.END, os.path.basename(file_path) + (" (Folder)" if os.path.isdir(file_path) else ""))
                            loaded_count += 1
                        elif not os.path.exists(file_path):
                            logging.warning(f"Skipped non-existent path during load: {file_path}")
                        elif file_path in self.file_list:
                            logging.info(f"Skipped duplicate path during load: {file_path}")

                self.update_shred_button()
                logging.info(f"File list loaded from: {filepath}. Loaded {loaded_count} items.")
                messagebox.showinfo("Success", f"File list loaded successfully! {loaded_count} valid items added.")
                self.update_status(f"Loaded list from: {os.path.basename(filepath)} ({loaded_count} items)")
            except Exception as e:
                logging.error(f"Error loading list: {e}")
                messagebox.showerror("Error", f"Error loading list:\n{e}")
                self.update_status("Failed to load list.")

    def start_shredding_thread(self):
        """Starts the shredding process in a separate thread."""
        selected_indices = self.listbox.curselection()
        if not selected_indices:
            messagebox.showinfo("No Selection", "Please select items from the list to shred.")
            return

        confirm = messagebox.askyesno(
            "Confirm Permanent Deletion",
            "Are you absolutely sure you want to PERMANENTLY DELETE the selected items? This action cannot be undone."
        )
        if not confirm:
            self.update_status("Shredding cancelled by user.")
            return

        # Disable all relevant UI elements during shredding
        self.enable_buttons(False)
        self.update_status("Shredding in progress...")

        # Get actual paths from the list based on selected indices
        items_to_shred = [self.file_list[index] for index in selected_indices]

        # Start shredding in a daemon thread
        threading.Thread(target=self.shred_items_worker, args=(items_to_shred, selected_indices), daemon=True).start()

    def enable_buttons(self, enable=True):
        """Enables or disables all primary UI buttons."""
        state = tk.NORMAL if enable else tk.DISABLED
        self.browse_button.config(state=state)
        self.folder_button.config(state=state)
        self.clear_button.config(state=state)
        self.remove_selected_button.config(state=state)
        self.open_button.config(state=state)
        self.save_button.config(state=state)
        self.load_button.config(state=state)
        self.mode_dropdown.config(state=state)
        # Shred button state is handled by update_shred_button separately
        if enable:
            self.update_shred_button()

    def shred_items_worker(self, items_to_shred, original_selected_indices):
        """Worker function for shredding, run in a separate thread."""
        progress_window = None
        successful_shreds = []
        failed_shreds = []
        ran_trim = 0
        failed_trim = 0
        try:
            # Create progress window on the main thread
            progress_window = tk.Toplevel(self)
            progress_window.title("Shredding Progress")
            progress_window.geometry("500x250")
            progress_window.resizable(False, False)
            # Prevent closing during shredding
            progress_window.protocol("WM_DELETE_WINDOW", lambda: messagebox.showwarning("Warning", "Please wait for shredding to complete or implement a 'Cancel' button."))

            ttk.Label(progress_window, text="Overall Progress:", font=('Segoe UI', 10, 'bold')).pack(pady=(10, 0))
            overall_progress = ttk.Progressbar(progress_window, length=400, mode='determinate')
            overall_progress.pack(pady=5)

            ttk.Label(progress_window, text="Current Item Progress:", font=('Segoe UI', 10, 'bold')).pack(pady=(10, 0))
            item_progress = ttk.Progressbar(progress_window, length=400, mode='determinate')
            item_progress.pack(pady=5)

            status_label = ttk.Label(progress_window, text="Initializing...", font=('Segoe UI', 9))
            status_label.pack(pady=10)

            # Calculate total files for overall progress
            total_items_count = 0
            for path in items_to_shred:
                if os.path.isfile(path):
                    total_items_count += 1
                elif os.path.isdir(path):
                    # Count all files in subdirectories
                    for _, _, files in os.walk(path):
                        total_items_count += len(files)
            
            # Ensure maximum is at least 1 to avoid division by zero if no files are found
            if total_items_count == 0:
                overall_progress['maximum'] = 1
            else:
                overall_progress['maximum'] = total_items_count

            current_shredded_files_count = 0
            
            mode = self.shred_mode.get()
            passes = 1
            if mode == "Normal Mode (1 Pass)":
                passes = 1 # Overwrite with random data
            elif mode == "Normal Mode (3 Pass)":
                passes = 3 # DoD 5220.22-M like
            elif mode == "Expert Mode (10 Pass)": # Corrected to 10 Pass
                passes = 10 # More passes for higher security

            for item_path in items_to_shred:
                if self.is_protected_path(item_path):
                    self.after(0, lambda p=item_path: messagebox.showerror("Error", f"Cannot shred protected system path: {p}"))
                    failed_shreds.append(item_path)
                    continue

                try:
                    if os.path.isfile(item_path):
                        self.after(0, lambda s=item_path: status_label.config(text=f"Shredding File: {os.path.basename(s)}"))
                        self.shred_file_contents(item_path, passes, item_progress)
                        os.remove(item_path) # Final deletion after shredding contents
                        successful_shreds.append(item_path)
                        current_shredded_files_count += 1
                        self.after(0, lambda: overall_progress.config(value=current_shredded_files_count))
                        logging.info(f"Successfully shredded and removed: {item_path}")

                    elif os.path.isdir(item_path):
                        self.after(0, lambda s=item_path: status_label.config(text=f"Processing Folder: {os.path.basename(s)}"))
                        # Walk from the deepest files/dirs up for safer deletion
                        for root, dirs, files in os.walk(item_path, topdown=False):
                            for name in files:
                                file_path = os.path.join(root, name)
                                if self.is_protected_path(file_path):
                                    self.after(0, lambda p=file_path: messagebox.showerror("Error", f"Cannot shred protected system file within folder: {p}"))
                                    failed_shreds.append(file_path)
                                    continue # Skip this file

                                self.after(0, lambda s=file_path: status_label.config(text=f"Shredding File in Folder: {os.path.basename(s)}"))
                                try:
                                    self.shred_file_contents(file_path, passes, item_progress)
                                    os.remove(file_path)
                                    successful_shreds.append(file_path)
                                    current_shredded_files_count += 1
                                    self.after(0, lambda: overall_progress.config(value=current_shredded_files_count))
                                    logging.info(f"Successfully shredded and removed: {file_path}")
                                except Exception as e:
                                    logging.error(f"Error shredding file {file_path} in folder: {e}")
                                    failed_shreds.append(file_path)
                                    self.after(0, lambda s=file_path, err=e: self.update_status(f"Error shredding {os.path.basename(s)}: {err}"))

                            for name in dirs:
                                dir_path = os.path.join(root, name)
                                try:
                                    os.rmdir(dir_path) # Remove empty directories
                                    logging.info(f"Removed empty directory: {dir_path}")
                                except OSError as e:
                                    logging.warning(f"Could not remove directory {dir_path}: {e}. It might not be empty or still in use.")
                                    # Don't add to failed_shreds here, as rmtree below might handle it

                        # After processing all contents, attempt to remove the top-level directory
                        try:
                            shutil.rmtree(item_path)
                            successful_shreds.append(item_path) # Mark folder as successfully removed
                            logging.info(f"Successfully removed folder: {item_path}")
                        except Exception as e:
                            logging.error(f"Error removing top-level folder {item_path}: {e}")
                            failed_shreds.append(item_path)
                            self.after(0, lambda s=item_path, err=e: messagebox.showerror("Error", f"Failed to remove folder {os.path.basename(s)}:\n{err}"))


                except Exception as e:
                    logging.error(f"Error during shredding of {item_path}: {e}", exc_info=True)
                    failed_shreds.append(item_path)
                    self.after(0, lambda p=item_path, err=e: messagebox.showerror("Error", f"An error occurred while shredding {os.path.basename(p)}:\n{err}"))

            # After all items are shredded and deleted, run TRIM on affected SSD drives
            if successful_shreds and self.trim_var.get():
                ran_trim, failed_trim = self._trim_affected_ssd_drives(successful_shreds)

        finally: # <--- Added this finally block
            # Post-shredding cleanup and UI updates on the main thread
            self.after(0, self._update_listbox_after_shredding, successful_shreds)
            self.after(0, self.enable_buttons, True) # Re-enable buttons

            if progress_window:
                self.after(0, progress_window.destroy) # Close progress window

            # Show final results
            if successful_shreds:
                trim_note = ""
                if failed_trim:
                    trim_note = (f"\n\nTRIM: {ran_trim} SSD drive(s) trimmed, {failed_trim} failed. "
                                 "Retrimming an SSD may require running this app as Administrator.")
                elif ran_trim:
                    trim_note = f"\n\nTRIM completed on {ran_trim} SSD drive(s) to help purge deleted data."
                messagebox.showinfo("Shredding Complete",
                                    f"Shredding process finished.\n"
                                    f"Successfully shredded {len(successful_shreds)} items.\n"
                                    f"{len(failed_shreds)} items failed.{trim_note}")
                self.update_status(f"Shredding completed. {len(successful_shreds)} success, {len(failed_shreds)} failed.")
            else:
                messagebox.showerror("Shredding Failed", "No items were successfully shredded.")
                self.update_status("Shredding failed.")


    def shred_file_contents(self, file_path, passes, item_progress):
        """Overwrites the contents of a single file multiple times."""
        try:
            file_size = os.path.getsize(file_path)
        except OSError as e:
            logging.error(f"Could not get size of {file_path}: {e}")
            raise # Re-raise to be caught by the caller

        if file_size == 0:
            logging.info(f"Skipping empty file: {file_path}")
            self.after(0, lambda: item_progress.config(value=item_progress['maximum'])) # Mark as complete
            return

        # Set item_progress maximum for the current file
        self.after(0, lambda: item_progress.config(maximum=passes * file_size, value=0))

        bytes_written_this_file = 0
        try:
            with open(file_path, 'r+b') as f:
                for p in range(passes):
                    f.seek(0) # Go to the beginning of the file for each pass
                    chunk_size = 65536 # Larger chunk size for better performance (64KB)

                    for i in range(0, file_size, chunk_size):
                        bytes_to_write = min(chunk_size, file_size - i)
                        if self.shred_mode.get() == "Basic Mode":
                            # Overwrite with null bytes (0x00)
                            data = b'\0' * bytes_to_write
                        else:
                            # Overwrite with random bytes
                            data = bytes([random.randint(0, 255) for _ in range(bytes_to_write)])
                        f.write(data)

                        bytes_written_this_file += bytes_to_write
                        # Update progress on the main thread
                        self.after(0, lambda current_val=bytes_written_this_file: item_progress.config(value=current_val))
                    f.flush() # Ensure data is written to disk after each pass
                    os.fsync(f.fileno()) # Force OS to sync data to disk (more robust)
        except Exception as e:
            logging.error(f"Failed to shred contents of {file_path}: {e}")
            raise # Re-raise to be caught by the calling shred_items_worker

    def _update_listbox_after_shredding(self, successful_shreds):
        """
        Updates the main listbox and internal file_list after shredding completes.
        Runs on the main Tkinter thread.
        """
        successful_shreds_set = set(os.path.normpath(p) for p in successful_shreds)

        new_file_list = []
        new_listbox_items = []

        # Iterate through the original list and keep only items not successfully shredded
        for i, path in enumerate(self.file_list):
            if os.path.normpath(path) not in successful_shreds_set:
                new_file_list.append(path)
                new_listbox_items.append(self.listbox.get(i)) # Get the display text

        self.file_list = new_file_list
        self.listbox.delete(0, tk.END) # Clear existing listbox
        for item_text in new_listbox_items:
            self.listbox.insert(tk.END, item_text) # Insert remaining items

        self.update_shred_button() # Update button states based on new listbox content


    def is_protected_path(self, path):
        """
        Checks if a given path is a critical system path or within the application's own directory.
        Prevents accidental self-destruction or system damage.
        """
        protected_paths = [
            "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)", "C:\\PerfLogs",
            "C:\\System Volume Information", "C:\\$Recycle.Bin",
            os.path.abspath(sys.executable) # Protect the Python executable itself
        ]

        abs_path = os.path.abspath(path)
        normalized_path = os.path.normpath(abs_path).lower() # Normalize and lowercase for case-insensitive comparison

        for protected in protected_paths:
            norm_prot = os.path.normpath(os.path.abspath(protected)).lower()
            if normalized_path == norm_prot or normalized_path.startswith(norm_prot + os.sep.lower()):
                logging.warning(f"Attempt to shred protected path detected: {path}")
                return True
        return False

    def get_drive_letter(self, path):
        """Extracts the drive letter (e.g. 'C') from a local Windows path."""
        if platform.system() != "Windows":
            return None
        try:
            drive = os.path.splitdrive(os.path.abspath(path))[0]
        except ValueError:
            return None
        if len(drive) == 2 and drive.endswith(":"):
            return drive[0].upper()
        return None

    def detect_ssd_drives(self):
        """Determines which local drive letters are solid-state drives (SSDs)."""
        if self._ssd_drives_cache is not None:
            return self._ssd_drives_cache

        ssd_drives = set()
        if platform.system() == "Windows":
            ps_script = """
            $result = @()
            Get-Volume | Where-Object { $_.DriveLetter } | ForEach-Object {
                $letter = $_.DriveLetter
                try {
                    $pd = Get-Partition -DriveLetter $letter | Get-Disk | Get-PhysicalDisk
                    if ($pd.MediaType -eq 'SSD') { $result += $letter }
                } catch { }
            }
            $result | Sort-Object -Unique
            """
            try:
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                    capture_output=True, text=True, timeout=60
                )
                if proc.returncode == 0:
                    for line in proc.stdout.splitlines():
                        letter = line.strip().rstrip(":")
                        if re.fullmatch(r"[A-Za-z]", letter):
                            ssd_drives.add(letter.upper())
                else:
                    logging.warning(f"SSD detection failed: {proc.stderr.strip()}")
            except Exception as e:
                logging.warning(f"Error during SSD detection: {e}")

        self._ssd_drives_cache = ssd_drives
        logging.info(f"SSD drives detected: {sorted(ssd_drives) if ssd_drives else 'none'}")
        return ssd_drives

    def run_trim(self, drive_letter):
        """Issues a TRIM (retrim) command to the specified SSD drive."""
        ps_script = f"""
        try {{
            Optimize-Volume -DriveLetter {drive_letter} -ReTrim -ErrorAction Stop
            "OK"
        }} catch {{
            "ERR: " + $_.Exception.Message
            exit 1
        }}
        """
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
                capture_output=True, text=True, timeout=600
            )
            if proc.returncode == 0 and "OK" in proc.stdout:
                logging.info(f"TRIM completed on drive {drive_letter}:")
                return True
            logging.warning(f"TRIM via Optimize-Volume failed on {drive_letter}: {proc.stderr.strip() or proc.stdout.strip()}")
        except Exception as e:
            logging.warning(f"Error running TRIM via Optimize-Volume on {drive_letter}: {e}")

        try:
            proc = subprocess.run(["defrag.exe", f"{drive_letter}:", "/L"],
                                  capture_output=True, text=True, timeout=900)
            if proc.returncode == 0:
                logging.info(f"TRIM completed on drive {drive_letter}: via defrag")
                return True
            logging.warning(f"TRIM via defrag failed on {drive_letter}: {proc.stderr.strip() or proc.stdout.strip()}")
        except Exception as e:
            logging.warning(f"Error running TRIM via defrag on {drive_letter}: {e}")
        return False

    def _trim_affected_ssd_drives(self, successful_paths):
        """Runs TRIM once per SSD drive that had successfully shredded files."""
        affected_drives = set()
        for path in successful_paths:
            drive = self.get_drive_letter(path)
            if drive:
                affected_drives.add(drive)
        if not affected_drives:
            return 0, 0

        targets = affected_drives & self.detect_ssd_drives()
        if not targets:
            logging.info("No affected drives are SSDs; TRIM not needed.")
            return 0, 0

        ran = 0
        failed = 0
        for drive in sorted(targets):
            self.after(0, lambda d=drive: self.update_status(f"Running TRIM on SSD drive {d}:..."))
            logging.info(f"Running TRIM on SSD drive {drive}:...")
            if self.run_trim(drive):
                ran += 1
            else:
                failed += 1
            self.after(0, lambda d=drive: self.update_status(f"TRIM finished on SSD drive {d}."))
        return ran, failed

if __name__ == "__main__":
    # Create the Tkinter root window and enable TkinterDND functionality
    app = FileListApp()
    app.mainloop()