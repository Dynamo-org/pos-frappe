### Restaurant POS

Multi-tenant restaurant POS integrating with ERPNext

### Installation

On **Frappe Cloud**: this app needs `erpnext` and `aurora_ui` on the bench first, and is published from its own repository (this folder is exported by `tools/frappe-export/export-frappe-app.mjs`). See `docs/runbooks/frappe-cloud-setup.md`.

On a self-managed bench, you can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app restaurant_pos
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/restaurant_pos
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
