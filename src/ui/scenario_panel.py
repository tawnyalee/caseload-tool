# This file builds the UI panel to display groups, insert, update (rename), and delete groups, insert, edit, and delete actions.
import customtkinter as ctk
from src.models.group import Group

class ScenarioNavigationPanel(ctk.CTkFrame):
    def __init__(self, master, groups: list[Group], scenarios_raw: dict, app_instance=None, **kwargs):
        super().__init__(master, **kwargs)
        self.groups = groups
        self.scenarios_raw = scenarios_raw
        self.app_instance = app_instance  # Reference back to launcher.py for background actions
        
        # Build the names array for the ComboBox, starting with "General"
        self.group_names = ["General"] + [group.name for group in self.groups]
        
        self._setup_ui()

    def _setup_ui(self):
        """Builds the operational links, shifted group dropdown layout, and actions grid."""
        
        # --- Top Section: Live Operational Links ---
        self.sync_bar_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.sync_bar_frame.pack(fill="x", padx=10, pady=(10, 5))

        # ⛔ STOP Link (Red Text)
        self.stop_link = ctk.CTkButton(
            self.sync_bar_frame, text="⛔ STOP Process", width=100,
            fg_color="transparent", text_color="#c0392b", hover=False,
            font=ctk.CTkFont(weight="bold"),
            command=lambda: self.app_instance._request_stop() if self.app_instance else print("STOP clicked")
        )
        self.stop_link.pack(side="left", padx=(0, 10))

        # ↻ Refresh Caseload Link
        self.refresh_link = ctk.CTkButton(
            self.sync_bar_frame, text="↻ Refresh Caseload", width=110,
            fg_color="transparent", text_color="#1f538d", hover=False,
            command=lambda: self.app_instance._on_caseload_refresh_clicked() if self.app_instance else print("Refresh clicked")
        )
        self.refresh_link.pack(side="left", padx=10)

        # ⬇ Sync Texting IDs Link
        self.sync_ids_link = ctk.CTkButton(
            self.sync_bar_frame, text="⬇ Sync Texting IDs", width=110,
            fg_color="transparent", text_color="#1f538d", hover=False,
            command=lambda: self.app_instance._sync_contact_ids() if self.app_instance else print("Sync IDs clicked")
        )
        self.sync_ids_link.pack(side="left", padx=10)

        # ↻ Restart Browser Link (Moved from bottom)
        self.browser_link = ctk.CTkButton(
            self.sync_bar_frame, text="↻ Restart Browser", width=110,
            fg_color="transparent", text_color="#1f538d", hover=False,
            command=lambda: self.app_instance._restart_browser() if self.app_instance else print("Browser restart clicked")
        )
        self.browser_link.pack(side="left", padx=10)

        # Subtle structural separator line
        self.separator = ctk.CTkFrame(self, height=2, fg_color=("#dbdbdb", "#2e2e2e"))
        self.separator.pack(fill="x", padx=10, pady=5)

        # --- Middle Section: Groups Navigation ---
        self.label = ctk.CTkLabel(
            self, text="Scenario Groups", font=ctk.CTkFont(size=14, weight="bold")
        )
        self.label.pack(padx=10, pady=(5, 2), anchor="w")

        # The Dropdown (Full Width Container)
        self.dropdown_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.dropdown_frame.pack(fill="x", padx=10, pady=(0, 4))
        
        self.group_dropdown = ctk.CTkComboBox(
            self.dropdown_frame, 
            values=self.group_names,
            command=self._on_group_selected,
        )
        self.group_dropdown.pack(fill="x", expand=True)
        self.group_dropdown.set("General")

        # CRUD Management Row placed IMMEDIATELY BELOW the Dropdown
        self.crud_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.crud_frame.pack(fill="x", padx=10, pady=(0, 15))

        self.add_group_btn = ctk.CTkButton(self.crud_frame, text="+ Add Group", width=90, command=self._add_group)
        self.add_group_btn.pack(side="left", padx=(0, 4))

        self.rename_group_btn = ctk.CTkButton(self.crud_frame, text="📝 Rename", width=90, command=self._rename_group)
        self.rename_group_btn.pack(side="left", padx=4)

        self.delete_group_btn = ctk.CTkButton(self.crud_frame, text="❌ Delete", width=90, command=self._delete_group)
        self.delete_group_btn.pack(side="left", padx=4)

        # Dynamic Section Header Label (e.g. "General - Actions")
        self.actions_header_label = ctk.CTkLabel(
            self, 
            text="General - Actions", 
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w"
        )
        self.actions_header_label.pack(fill="x", padx=12, pady=(5, 2))

        # Bottom Section: The Table Container
        self.table_frame = ctk.CTkScrollableFrame(self)
        self.table_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        
        self.table_frame.grid_columnconfigure(0, weight=1)
        self.table_frame.grid_columnconfigure(1, weight=0)
        self.table_frame.grid_columnconfigure(2, weight=0)
        self.table_frame.grid_columnconfigure(3, weight=0)

        self._render_table("General")

    # --- Group Management Operations (Stubs for Refactoring Later) ---
    def _add_group(self):
        """Triggers a dialog to add a new group."""
        pass

    def _rename_group(self):
        """Renames the currently selected group."""
        current = self.group_dropdown.get()
        if current == "General":
            return
        pass

    def _delete_group(self):
        """Deletes the currently selected group if it has no actions."""
        current = self.group_dropdown.get()
        if current == "General":
            return
        pass

    # --- Action Item Click Event Stubs ---
    def _on_action_click(self, action_name: str):
        """Fires when the main action button is pressed."""
        print(f"Triggered/Editing action: {action_name}")

    def _rename_action(self, action_name: str):
        """Fires when the rename icon for an action is pressed."""
        print(f"Renaming action: {action_name}")

    def _delete_action(self, action_name: str):
        """Fires when the delete icon for an action is pressed."""
        print(f"Deleting action: {action_name}")

    # --- Tabular Actions Grid ---
    def _on_group_selected(self, selected_group_name: str):
        # Update the header label dynamically when selection changes
        self.actions_header_label.configure(text=f"{selected_group_name} - Actions")
        self._render_table(selected_group_name)

    def _render_table(self, group_name: str):
        """Clears the previous grid rows and draws the new table rows."""
        for widget in self.table_frame.winfo_children():
            widget.destroy()
            
        # Determine which actions belong here
        actions_to_show = []
        
        if group_name == "General":
            # Collect all action names that DO NOT appear in any assigned group
            assigned_actions = set()
            for g in self.groups:
                assigned_actions.update(g.scenarios)
            
            actions_to_show = [name for name in self.scenarios_raw.keys() if name not in assigned_actions]
        else:
            # Find the specific group configuration block
            matched_group = next((g for g in self.groups if g.name == group_name), None)
            if matched_group:
                actions_to_show = matched_group.scenarios

        # Render rows for each action in a tabular format
        for row_idx, action_name in enumerate(actions_to_show):
            # Column 0: Action Name Label
            lbl = ctk.CTkLabel(self.table_frame, text=action_name, anchor="w")
            lbl.grid(row=row_idx, column=0, padx=(10, 20), pady=5, sticky="ew")
            
            # Column 1: Run/Edit Button
            btn_run = ctk.CTkButton(
                self.table_frame, 
                text="Run/Edit", 
                width=80, 
                command=lambda name=action_name: self._on_action_click(name)
            )
            btn_run.grid(row=row_idx, column=1, padx=2, pady=5, sticky="e")
            
            # Column 2: Rename Button
            btn_rename = ctk.CTkButton(
                self.table_frame, 
                text="📝", 
                width=35, 
                command=lambda name=action_name: self._rename_action(name)
            )
            btn_rename.grid(row=row_idx, column=2, padx=2, pady=5, sticky="e")
            
            # Column 3: Delete Button
            btn_delete = ctk.CTkButton(
                self.table_frame, 
                text="❌", 
                width=35, 
                fg_color="#8B0000", 
                hover_color="#660000",
                command=lambda name=action_name: self._delete_action(name)
            )
            btn_delete.grid(row=row_idx, column=3, padx=(2, 10), pady=5, sticky="e")