from __future__ import annotations

import threading
import tkinter as tk
import os
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from warehouse_analysis.rate_manager import (
    DEFAULT_RATE_DATABASE,
    add_rate_rule,
    diagnose_rate_rule_compatibility,
    disable_rate_rule,
    import_rate_config,
    load_rate_database,
    search_rate_rule,
    update_rate_rule,
)
from warehouse_analysis.service import run_analysis
from warehouse_analysis.io.erp_adapter import (
    ERPDetectionResult,
    detect_erp_file,
)
from warehouse_analysis.logging_config import configure_logging
from warehouse_analysis.user_settings import (
    existing_initial_directory,
    load_user_settings,
    save_user_settings,
)
from warehouse_analysis.version import version_label
from warehouse_analysis.auth import (
    AuthUser,
    ROLE_ADMIN,
    ROLE_FINANCE,
    authenticate,
    create_user,
    ensure_default_admin,
    has_permission,
    list_users,
)
from warehouse_analysis.audit import (
    query_audit,
    record_audit,
    record_warehouse_mapping_confirmation,
)
from warehouse_analysis.dashboard import load_dashboard_data
from warehouse_analysis.rate_governance import (
    approve_rate_change,
    query_rate_requests,
    query_rate_version_changes,
    submit_rate_change,
)
from warehouse_analysis.batch_runner import (
    append_run_history,
    build_history_record,
    load_run_history,
    run_batch_analysis,
)
from warehouse_analysis.history_db import query_history
from warehouse_analysis.io.erp_adapter import load_erp_inputs
from warehouse_analysis.io.excel_loader import load_separate_business_files
from warehouse_analysis.multi_warehouse import (
    identify_warehouses,
    run_multi_warehouse_analysis,
)


class WarehouseAnalysisApp:
    """只负责收集参数和展示服务结果的tkinter桌面界面。"""

    def __init__(self, root: tk.Tk, user: AuthUser | None = None) -> None:
        self.root = root
        self.user = user or AuthUser(0, "system", ROLE_ADMIN, "启用")
        self.root.title(version_label())
        self.root.geometry("880x740")
        self.root.minsize(800, 680)

        self.logger = configure_logging()
        self.logger.info("用户=%s | 角色=%s | 登录主界面", self.user.username, self.user.role)
        self.settings = load_user_settings()
        self.input_file = tk.StringVar()
        self.opening_inventory_file = tk.StringVar()
        self.rate_selection = tk.StringVar()
        self.erp_transaction_mapping: dict[str, str] = {}
        self.erp_opening_mapping: dict[str, str] = {}
        self.warehouse_name_mapping: dict[str, str] = {}
        self.erp_detection_text = tk.StringVar(
            value="ERP模式下请选择文件后执行字段检测。"
        )
        self.start_date = tk.StringVar(value="2025-01-01")
        self.end_date = tk.StringVar(value="2026-03-31")
        self.analysis_mode = tk.StringVar(value="单仓分析")
        self.start_month = tk.StringVar(value="2026-01")
        self.end_month = tk.StringVar(value="2026-12")
        self.output_dir = tk.StringVar(
            value=self.settings["last_output_dir"] or str(Path.cwd())
        )
        self.input_type = tk.StringVar(value="独立业务文件（默认）")
        self.status = tk.StringVar(value="请选择文件并填写统计期间。")
        if Path(self.settings["last_input_file"]).is_file():
            self.input_file.set(self.settings["last_input_file"])
        if Path(self.settings["last_opening_inventory"]).is_file():
            self.opening_inventory_file.set(
                self.settings["last_opening_inventory"]
            )
        self.rate_selection.set(self.settings["last_rate_rule"])
        self.logger.info("最近使用配置 | 加载成功")
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._build_menu()

        container = ttk.Frame(root, padding=20)
        container.pack(fill="both", expand=True)
        ttk.Label(
            container,
            text=version_label(),
            font=("Microsoft YaHei UI", 18, "bold"),
        ).grid(row=0, column=0, columnspan=3, pady=(0, 20), sticky="w")

        ttk.Label(container, text="输入类型").grid(
            row=1, column=0, padx=(0, 12), pady=7, sticky="e"
        )
        input_type_box = ttk.Combobox(
            container,
            textvariable=self.input_type,
            values=[
                "独立业务文件（默认）",
                "ERP原始流水",
                "历史三表合一Excel",
                "示例原始数据",
            ],
            state="readonly",
        )
        input_type_box.grid(row=1, column=1, pady=7, sticky="ew")
        self._file_row(
            container, 2, "出入库明细", self.input_file, self._choose_input
        )
        self._file_row(
            container,
            3,
            "期初库存",
            self.opening_inventory_file,
            self._choose_opening_inventory,
        )
        ttk.Label(
            container,
            textvariable=self.erp_detection_text,
            wraplength=520,
            foreground="#475569",
        ).grid(row=4, column=0, columnspan=2, pady=5, sticky="w")
        ttk.Button(
            container,
            text="确认导入",
            command=self._confirm_erp_import,
        ).grid(row=4, column=2, padx=(12, 0), pady=5)
        ttk.Label(container, text="系统费率").grid(
            row=5, column=0, padx=(0, 12), pady=7, sticky="e"
        )
        self.rate_box = ttk.Combobox(
            container, textvariable=self.rate_selection, state="readonly"
        )
        self.rate_box.grid(row=5, column=1, pady=7, sticky="ew")
        self.rate_box.bind("<<ComboboxSelected>>", self._on_rate_selected)
        ttk.Button(
            container, text="费率管理", command=self._open_rate_manager
        ).grid(row=5, column=2, padx=(12, 0), pady=7)
        self._refresh_rate_choices()
        self._entry_row(container, 6, "统计开始日期", self.start_date)
        self._entry_row(container, 7, "统计结束日期", self.end_date)
        self._file_row(
            container, 8, "输出目录", self.output_dir, self._choose_output
        )

        batch_options = ttk.Frame(container)
        batch_options.grid(row=9, column=0, columnspan=3, pady=7, sticky="ew")
        ttk.Label(batch_options, text="分析模式").pack(side="left")
        ttk.Combobox(
            batch_options,
            textvariable=self.analysis_mode,
            values=["单仓分析", "月度批量分析", "多仓批量分析"],
            state="readonly",
            width=14,
        ).pack(side="left", padx=(8, 18))
        ttk.Label(batch_options, text="开始月份").pack(side="left")
        ttk.Entry(
            batch_options, textvariable=self.start_month, width=10
        ).pack(side="left", padx=(8, 18))
        ttk.Label(batch_options, text="结束月份").pack(side="left")
        ttk.Entry(
            batch_options, textvariable=self.end_month, width=10
        ).pack(side="left", padx=(8, 0))

        self.run_button = ttk.Button(
            container, text="开始分析", command=self._start_analysis
        )
        self.run_button.grid(row=10, column=1, pady=18, sticky="ew")
        self.batch_button = ttk.Button(
            container,
            text="批量生成报告",
            command=self._start_batch_analysis,
        )
        self.batch_button.grid(
            row=10, column=2, padx=(12, 0), pady=18, sticky="ew"
        )
        ttk.Button(
            container,
            text="历史运行记录",
            command=self._open_history,
        ).grid(row=10, column=0, padx=(0, 12), pady=18, sticky="ew")
        self.multi_button = ttk.Button(
            container,
            text="运行多仓分析",
            command=self._start_multi_analysis,
        )
        self.multi_button.grid(row=11, column=1, pady=(0, 12), sticky="ew")
        ttk.Button(
            container,
            text="企业运行历史",
            command=self._open_enterprise_history,
        ).grid(row=11, column=2, padx=(12, 0), pady=(0, 12), sticky="ew")
        ttk.Separator(container).grid(
            row=12, column=0, columnspan=3, sticky="ew"
        )
        ttk.Label(
            container,
            textvariable=self.status,
            wraplength=690,
            foreground="#334155",
        ).grid(row=13, column=0, columnspan=3, pady=15, sticky="w")
        ttk.Label(container, text="运行日志").grid(
            row=14, column=0, columnspan=3, sticky="w"
        )
        self.log_text = tk.Text(
            container,
            height=8,
            wrap="word",
            state="disabled",
            font=("Microsoft YaHei UI", 9),
        )
        self.log_text.grid(
            row=15, column=0, columnspan=3, pady=(6, 0), sticky="nsew"
        )
        scrollbar = ttk.Scrollbar(
            container, orient="vertical", command=self.log_text.yview
        )
        scrollbar.grid(row=15, column=3, pady=(6, 0), sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(15, weight=1)
        if not has_permission(self.user, "run_analysis"):
            self.run_button.configure(state="disabled")
            self.batch_button.configure(state="disabled")
            self.multi_button.configure(state="disabled")

    def _build_menu(self) -> None:
        menu = tk.Menu(self.root)
        menu.add_command(label="首页")
        menu.add_command(
            label="运行分析",
            command=self._start_analysis,
            state="normal" if has_permission(self.user, "run_analysis") else "disabled",
        )
        menu.add_command(
            label="企业批量",
            command=self._start_multi_analysis,
            state="normal" if has_permission(self.user, "run_analysis") else "disabled",
        )
        menu.add_command(label="经营驾驶舱", command=self._open_dashboard)
        menu.add_command(
            label="费率管理",
            command=self._open_rate_manager,
            state="normal"
            if (
                has_permission(self.user, "manage_rates")
                or has_permission(self.user, "submit_rate_change")
            )
            else "disabled",
        )
        menu.add_command(
            label="历史记录",
            command=self._open_enterprise_history,
            state="normal" if has_permission(self.user, "view_history") else "disabled",
        )
        menu.add_command(
            label="用户管理",
            command=self._open_user_management,
            state="normal" if has_permission(self.user, "manage_users") else "disabled",
        )
        menu.add_command(
            label="系统日志",
            command=self._open_audit_log,
            state="normal" if has_permission(self.user, "view_logs") else "disabled",
        )
        self.root.config(menu=menu)

    def _check_run_permission(self) -> bool:
        if has_permission(self.user, "run_analysis"):
            return True
        messagebox.showerror("权限不足", "当前角色无权运行分析任务。")
        return False
        self._append_log("最近使用配置加载成功。")

    def _file_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, padx=(0, 12), pady=7, sticky="e")
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, pady=7, sticky="ew")
        ttk.Button(parent, text="选择…", command=command).grid(row=row, column=2, padx=(12, 0), pady=7)

    def _entry_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, padx=(0, 12), pady=7, sticky="e")
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, pady=7, sticky="ew")
        ttk.Label(parent, text="YYYY-MM-DD").grid(row=row, column=2, padx=(12, 0), pady=7, sticky="w")

    def _choose_input(self) -> None:
        path = filedialog.askopenfilename(
            title="选择出入库明细",
            initialdir=existing_initial_directory(
                self.settings["last_input_file"]
            ),
            filetypes=[("Excel文件", "*.xlsx *.xls"), ("所有文件", "*.*")],
        )
        if path:
            self.input_file.set(path)
            self.erp_transaction_mapping = {}
            self.settings["last_input_file"] = path
            self._save_settings()
            if self.input_type.get() == "ERP原始流水":
                self._preview_erp_detection(path, "transaction")

    def _choose_opening_inventory(self) -> None:
        path = filedialog.askopenfilename(
            title="选择期初库存",
            initialdir=existing_initial_directory(
                self.settings["last_opening_inventory"],
                Path(self.input_file.get()).parent
                if self.input_file.get()
                else Path.cwd(),
            ),
            filetypes=[("Excel文件", "*.xlsx *.xls"), ("所有文件", "*.*")],
        )
        if path:
            self.opening_inventory_file.set(path)
            self.erp_opening_mapping = {}
            self.warehouse_name_mapping = {}
            self.settings["last_opening_inventory"] = path
            self._save_settings()
            if self.input_type.get() == "ERP原始流水":
                self._preview_erp_detection(path, "opening")

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(
            title="选择报告输出目录",
            initialdir=existing_initial_directory(
                self.settings["last_output_dir"]
            ),
        )
        if path:
            self.output_dir.set(path)
            self.settings["last_output_dir"] = path
            self._save_settings()

    def _start_analysis(self) -> None:
        if not self._check_run_permission():
            return
        self.analysis_mode.set("单仓分析")
        if not self.input_file.get().strip():
            messagebox.showerror("输入错误", "请选择出入库明细文件。")
            return
        if (
            self.input_type.get() != "历史三表合一Excel"
            and not self.opening_inventory_file.get().strip()
        ):
            messagebox.showerror(
                "缺少期初库存",
                "缺少期初库存文件，无法计算FIFO库存及仓储费用，请上传期初库存。",
            )
            return
        if not self.output_dir.get().strip():
            messagebox.showerror("输入错误", "请选择输出目录。")
            return
        if not self.rate_selection.get().strip():
            messagebox.showerror("输入错误", "请选择仓库名称+计费规则。")
            return
        if not self._check_selected_rate_compatibility():
            return
        if self.input_type.get() == "ERP原始流水":
            if not self._confirm_erp_import():
                return
        elif self.input_type.get() == "独立业务文件（默认）":
            if not self._confirm_separate_opening_fields():
                return
        self.run_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.multi_button.configure(state="disabled")
        self.status.set("正在运行分析，请稍候……")
        self._append_log("开始运行。")
        arguments = (
            self.input_file.get().strip(),
            self.opening_inventory_file.get().strip(),
            self.start_date.get().strip(),
            self.end_date.get().strip(),
            self.output_dir.get().strip(),
            (
                "sichuan_raw"
                if self.input_type.get() == "四川力庆库原始数据"
                else (
                    "erp"
                    if self.input_type.get() == "ERP原始流水"
                    else (
                        "standard"
                        if self.input_type.get() == "历史三表合一Excel"
                        else "separate"
                    )
                )
            ),
            self.rate_selection.get().strip(),
            self.erp_transaction_mapping.copy(),
            self.erp_opening_mapping.copy(),
        )
        self.settings.update(
            {
                "last_input_file": arguments[0],
                "last_opening_inventory": arguments[1],
                "last_rate_rule": arguments[6],
                "last_output_dir": arguments[4],
            }
        )
        self._save_settings()
        threading.Thread(
            target=self._run_worker, args=arguments, daemon=True
        ).start()

    def _run_worker(
        self,
        input_file: str,
        opening_inventory_file: str,
        start_date: str,
        end_date: str,
        output_dir: str,
        input_type: str,
        rate_selection: str,
        erp_transaction_mapping: dict[str, str],
        erp_opening_mapping: dict[str, str],
    ) -> None:
        output_path = Path(output_dir)
        result = run_analysis(
            input_file,
            None,
            {
                "start": start_date,
                "end": end_date,
            },
            output_path,
            progress_callback=self._queue_log,
            input_type=input_type,
            rate_selection=rate_selection,
            opening_inventory_file=opening_inventory_file or None,
            erp_transaction_mapping=erp_transaction_mapping,
            erp_opening_mapping=erp_opening_mapping,
            warehouse_name_mapping=self.warehouse_name_mapping,
            opening_field_mapping=(
                {
                    key: value
                    for key, value in {
                        "期初数量": erp_opening_mapping.get("quantity"),
                        "期初批次入库基准日期": erp_opening_mapping.get(
                            "inventory_date"
                        ),
                    }.items()
                    if value
                }
                if input_type == "separate"
                else None
            ),
        )
        append_run_history(
            build_history_record(
                input_file=input_file,
                opening_inventory_file=opening_inventory_file or None,
                rate_selection=rate_selection,
                period=f"{start_date}至{end_date}",
                period_start=start_date,
                period_end=end_date,
                output_path=result.output_path or output_path,
                result=result,
                elapsed_seconds=float(
                    result.summary.get("运行耗时（秒）", 0)
                ),
            )
        )
        self.root.after(0, self._show_result, result)

    def _start_batch_analysis(self) -> None:
        if not self._check_run_permission():
            return
        self.analysis_mode.set("月度批量分析")
        if not self.input_file.get().strip():
            messagebox.showerror("输入错误", "请选择出入库明细文件。")
            return
        if (
            self.input_type.get() != "历史三表合一Excel"
            and not self.opening_inventory_file.get().strip()
        ):
            messagebox.showerror("输入错误", "请选择期初库存文件。")
            return
        if not self.rate_selection.get().strip():
            messagebox.showerror("输入错误", "请选择仓库名称+计费规则。")
            return
        if not self._check_selected_rate_compatibility():
            return
        if not self.output_dir.get().strip():
            messagebox.showerror("输入错误", "请选择输出目录。")
            return
        if self.input_type.get() == "ERP原始流水":
            if not self._confirm_erp_import():
                return
        input_type = (
            "erp"
            if self.input_type.get() == "ERP原始流水"
            else (
                "sichuan_raw"
                if self.input_type.get() == "四川力庆库原始数据"
                else (
                    "standard"
                    if self.input_type.get() == "历史三表合一Excel"
                    else "separate"
                )
            )
        )
        self.run_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.multi_button.configure(state="disabled")
        self.status.set("月度批量任务正在运行，请稍候……")
        threading.Thread(
            target=self._run_batch_worker,
            args=(
                self.input_file.get().strip(),
                self.opening_inventory_file.get().strip(),
                self.rate_selection.get().strip(),
                self.start_month.get().strip(),
                self.end_month.get().strip(),
                self.output_dir.get().strip(),
                input_type,
                self.erp_transaction_mapping.copy(),
                self.erp_opening_mapping.copy(),
            ),
            daemon=True,
        ).start()

    def _run_batch_worker(
        self,
        input_file: str,
        opening_inventory_file: str,
        rate_selection: str,
        start_month: str,
        end_month: str,
        output_dir: str,
        input_type: str,
        erp_transaction_mapping: dict[str, str],
        erp_opening_mapping: dict[str, str],
    ) -> None:
        try:
            result = run_batch_analysis(
                input_file,
                opening_inventory_file or None,
                rate_selection,
                start_month,
                end_month,
                output_dir,
                input_type=input_type,
                erp_transaction_mapping=erp_transaction_mapping,
                erp_opening_mapping=erp_opening_mapping,
                progress_callback=self._queue_log,
            )
            self.root.after(0, self._show_batch_result, result)
        except Exception as exc:
            self.root.after(
                0,
                self._show_batch_error,
                str(exc),
            )

    def _show_batch_result(self, result) -> None:
        self.run_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self.multi_button.configure(state="normal")
        message = (
            f"批量任务完成：成功{result.success_count}个月，"
            f"失败{result.failure_count}个月。\n"
            f"报告目录：{Path(self.output_dir.get()) / 'reports'}"
        )
        self.status.set(message)
        record_audit(
            user=self.user.username,
            action="分析运行",
            target="月度批量",
            before={"开始月份": self.start_month.get(), "结束月份": self.end_month.get()},
            after={"成功": result.success_count, "失败": result.failure_count},
            result="成功" if result.failure_count == 0 else "部分成功",
        )
        messagebox.showinfo("批量分析完成", message)

    def _show_batch_error(self, message: str) -> None:
        self.run_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self.multi_button.configure(state="normal")
        self.status.set(message)
        messagebox.showerror("批量分析失败", message)

    def _queue_log(self, message: str) -> None:
        self.root.after(0, self._append_log, message)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _show_result(self, result) -> None:
        self.run_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self.multi_button.configure(state="normal")
        if result.success:
            report_path = Path(result.output_path)
            warehouse = str(
                result.summary.get("仓库名称", "仓库")
            )
            message = (
                f"{warehouse}分析完成\n\n"
                f"报告：\n{report_path.name}\n\n"
                f"路径：\n{report_path.parent}\n\n"
                f"仓储费总额：{result.summary.get('仓储费总额（元）', 0):,.2f}元\n"
                f"运行耗时：{result.summary.get('运行耗时（秒）', 0):,.2f}秒"
            )
            self.status.set(message)
            record_audit(
                user=self.user.username,
                action="分析运行",
                target=self.rate_selection.get(),
                before={
                    "输入文件": self.input_file.get(),
                    "期间": f"{self.start_date.get()}至{self.end_date.get()}",
                },
                after={"输出文件": str(result.output_path), "仓储费": result.summary.get("仓储费总额（元）", 0)},
                result="成功",
            )
            messagebox.showinfo("分析完成", message)
        else:
            message = result.error or "计算失败，请检查输入文件。"
            self.status.set(message)
            record_audit(
                user=self.user.username,
                action="分析运行",
                target=self.rate_selection.get(),
                before={"输入文件": self.input_file.get()},
                after={"错误": message},
                result="失败",
            )
            diagnosis = result.summary.get("费率诊断", {})
            if diagnosis:
                candidates = diagnosis.get("候选列表", [])
                if candidates:
                    candidate = candidates[0]
                    confirmed = messagebox.askokcancel(
                        "发现可能匹配",
                        "发现可能匹配：\n\n"
                        f"ERP仓库：\n{candidate.get('原仓库', '')}\n\n"
                        f"费率库：\n{candidate.get('候选费率', '')}\n\n"
                        f"相似度：{candidate.get('相似度', 0)}%\n\n"
                        "是否使用该费率？\n\n确认使用 / 取消",
                    )
                    if confirmed:
                        source = str(candidate.get("原仓库", ""))
                        target = str(candidate.get("候选费率", ""))
                        self.warehouse_name_mapping[source] = target
                        record_warehouse_mapping_confirmation(
                            source, target, user=self.user.username
                        )
                        self._append_log(
                            f"用户确认仓库名称映射：{source} → {target}"
                        )
                        self._start_analysis()
                    return
                enter_manager = messagebox.askyesno(
                    "费率匹配失败",
                    f"{message}\n\n"
                    "是否立即打开【创建费率规则】？\n"
                    "选择“否”将停止本次运行；多仓批量模式会保留异常并继续其他仓库。",
                )
                if enter_manager:
                    RateManagerWindow(
                        self.root,
                        self._refresh_rate_choices,
                        self.user,
                        prefill={
                            "仓库名称": diagnosis.get("仓库名称", ""),
                            "物料名称": diagnosis.get("物料名称", ""),
                            "品类": diagnosis.get("品类", ""),
                            "生效日期": diagnosis.get("统计日期", ""),
                        },
                    )
            else:
                messagebox.showerror("分析失败", message)

    def _refresh_rate_choices(self) -> None:
        try:
            choices = search_rate_rule("")["选择项"].tolist()
        except Exception as exc:
            choices = []
            self._append_log(f"费率库读取失败：{exc}") if hasattr(self, "log_text") else None
        self.rate_box["values"] = choices
        if choices and self.rate_selection.get() not in choices:
            preferred = next(
                (item for item in choices if item.startswith("示例仓库+")),
                choices[0],
            )
            self.rate_selection.set(preferred)

    def _on_rate_selected(self, _event=None) -> None:
        self._check_selected_rate_compatibility()

    def _check_selected_rate_compatibility(self) -> bool:
        selection = self.rate_selection.get().strip()
        if not selection:
            return False
        try:
            diagnosis = diagnose_rate_rule_compatibility(
                selection, self.end_date.get().strip()
            )
        except Exception as exc:
            messagebox.showerror("费率规则检查失败", str(exc))
            return False
        if diagnosis.get("可自动计算"):
            return True
        choose_compatible = messagebox.askyesno(
            "费率规则需要业务确认",
            "该费率规则包含存放天数分档，目前冻结计算核心不支持自动计算。\n\n"
            f"仓库：{diagnosis.get('仓库', '')}\n"
            f"规则：{diagnosis.get('规则名称', '')}\n"
            f"规则层级：{diagnosis.get('规则层级', '')}\n"
            f"当前规则类型：{diagnosis.get('当前规则类型', '')}\n\n"
            "请选择：\n"
            "是：返回并选择兼容统一单价规则继续计算\n"
            "否：打开费率管理维护规则\n\n"
            "系统不会自动修改或采用其他规则。",
        )
        if choose_compatible:
            self.rate_box.focus_set()
        else:
            self._open_rate_manager()
        return False

    def _save_settings(self) -> None:
        try:
            save_user_settings(self.settings)
            self.logger.info("最近使用配置 | 保存成功")
        except OSError as exc:
            self.logger.warning("最近使用配置 | 保存失败 | %s", exc)

    def _close(self) -> None:
        self.settings.update(
            {
                "last_input_file": self.input_file.get().strip(),
                "last_opening_inventory": self.opening_inventory_file.get().strip(),
                "last_rate_rule": self.rate_selection.get().strip(),
                "last_output_dir": self.output_dir.get().strip(),
            }
        )
        self._save_settings()
        self.root.destroy()

    def _open_rate_manager(self) -> None:
        RateManagerWindow(self.root, self._refresh_rate_choices, self.user)

    def _open_history(self) -> None:
        RunHistoryWindow(self.root)

    def _open_enterprise_history(self) -> None:
        EnterpriseHistoryWindow(self.root)

    def _open_dashboard(self) -> None:
        DashboardWindow(self.root)

    def _open_user_management(self) -> None:
        UserManagementWindow(self.root, self.user)

    def _open_audit_log(self) -> None:
        AuditLogWindow(self.root)

    def _start_multi_analysis(self) -> None:
        if not self._check_run_permission():
            return
        self.analysis_mode.set("多仓批量分析")
        input_file = self.input_file.get().strip()
        opening_file = self.opening_inventory_file.get().strip()
        if not input_file or not opening_file:
            messagebox.showerror(
                "多仓输入错误", "请选择ERP流水和期初库存文件。"
            )
            return
        input_type = (
            "erp" if self.input_type.get() == "ERP原始流水" else "separate"
        )
        if input_type == "erp" and not self._confirm_erp_import():
            return
        try:
            if input_type == "erp":
                transactions, openings, _, _ = load_erp_inputs(
                    input_file,
                    opening_file,
                    transaction_mapping=self.erp_transaction_mapping,
                    opening_mapping=self.erp_opening_mapping,
                    period_start=self.start_date.get().strip(),
                )
            else:
                transactions, openings = load_separate_business_files(
                    input_file, opening_file
                )
            warehouses = identify_warehouses(transactions, openings)
        except Exception as exc:
            messagebox.showerror("仓库识别失败", str(exc))
            return
        selection_window = WarehouseSelectionWindow(self.root, warehouses)
        self.root.wait_window(selection_window.window)
        if not selection_window.result:
            return
        self.run_button.configure(state="disabled")
        self.batch_button.configure(state="disabled")
        self.multi_button.configure(state="disabled")
        threading.Thread(
            target=self._run_multi_worker,
            args=(
                input_file,
                opening_file,
                input_type,
                selection_window.result,
            ),
            daemon=True,
        ).start()

    def _run_multi_worker(
        self,
        input_file: str,
        opening_file: str,
        input_type: str,
        warehouses: list[str],
    ) -> None:
        try:
            result = run_multi_warehouse_analysis(
                input_file,
                opening_file,
                {
                    "name": (
                        self.start_date.get().strip()
                        + "至"
                        + self.end_date.get().strip()
                    ),
                    "start": self.start_date.get().strip(),
                    "end": self.end_date.get().strip(),
                },
                self.output_dir.get().strip(),
                input_type=input_type,
                selected_warehouses=warehouses,
                erp_transaction_mapping=self.erp_transaction_mapping,
                erp_opening_mapping=self.erp_opening_mapping,
                progress_callback=self._queue_log,
            )
            self.root.after(0, self._show_multi_result, result)
        except Exception as exc:
            self.root.after(0, self._show_multi_error, str(exc))

    def _show_multi_result(self, result) -> None:
        self.run_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self.multi_button.configure(state="normal")
        success_lines = [
            f"√ {record['warehouse']}\n  {record.get('report_filename', '')}"
            for record in result.records
            if record["status"] == "success"
        ]
        failure_lines = [
            f"× {record['warehouse']}\n  {record.get('error', '')}"
            for record in result.records
            if record["status"] != "success"
        ]
        message = (
            "批量运行完成\n\n成功：\n"
            + ("\n\n".join(success_lines) if success_lines else "0个")
            + "\n\n失败：\n"
            + ("\n\n".join(failure_lines) if failure_lines else "0个")
            + f"\n\n企业汇总：\n{result.company_report_path}"
        )
        self.status.set(message)
        self.logger.info(
            "用户=%s | 多仓分析完成 | 成功=%s | 失败=%s",
            self.user.username,
            result.success_count,
            result.failure_count,
        )
        record_audit(
            user=self.user.username,
            action="分析运行",
            target="企业多仓",
            before={"期间": f"{self.start_date.get()}至{self.end_date.get()}"},
            after={"成功": result.success_count, "失败": result.failure_count, "企业汇总": str(result.company_report_path)},
            result="成功" if result.failure_count == 0 else "部分成功",
        )
        messagebox.showinfo("企业多仓分析完成", message)

    def _show_multi_error(self, message: str) -> None:
        self.run_button.configure(state="normal")
        self.batch_button.configure(state="normal")
        self.multi_button.configure(state="normal")
        self.status.set(message)
        record_audit(
            user=self.user.username,
            action="分析运行",
            target="企业多仓",
            after={"错误": message},
            result="失败",
        )
        messagebox.showerror("企业多仓分析失败", message)

    def _preview_erp_detection(self, path: str, table_kind: str) -> None:
        try:
            detection = detect_erp_file(path, table_kind=table_kind)
            mapping = detection.resolved_mapping
            label = "流水" if table_kind == "transaction" else "期初"
            pairs = "，".join(
                f"{field}→{column}" for field, column in mapping.items()
            )
            ambiguous = detection.ambiguous_fields
            suffix = (
                "；待确认：" + "，".join(ambiguous)
                if ambiguous
                else ""
            )
            self.erp_detection_text.set(
                f"{label}检测：{Path(path).name} | Sheet："
                f"{detection.sheet_name} | {pairs}{suffix}"
            )
        except Exception as exc:
            self.erp_detection_text.set(f"ERP检测失败：{exc}")

    def _confirm_one_erp_file(
        self, path: str, table_kind: str
    ) -> dict[str, str] | None:
        try:
            detection = detect_erp_file(path, table_kind=table_kind)
        except Exception as exc:
            messagebox.showerror("ERP字段检测失败", str(exc))
            return None
        dialog = ERPFieldConfirmationWindow(self.root, detection)
        self.root.wait_window(dialog.window)
        return dialog.result

    def _confirm_erp_import(self) -> bool:
        if self.input_type.get() != "ERP原始流水":
            return True
        if self.erp_transaction_mapping and self.erp_opening_mapping:
            return True
        input_path = self.input_file.get().strip()
        opening_path = self.opening_inventory_file.get().strip()
        if not input_path or not opening_path:
            messagebox.showerror(
                "ERP导入错误", "请先选择ERP流水和ERP期初库存文件。"
            )
            return False
        transaction_mapping = self._confirm_one_erp_file(
            input_path, "transaction"
        )
        if transaction_mapping is None:
            return False
        opening_mapping = self._confirm_one_erp_file(
            opening_path, "opening"
        )
        if opening_mapping is None:
            return False
        self.erp_transaction_mapping = transaction_mapping
        self.erp_opening_mapping = opening_mapping
        self.erp_detection_text.set(
            "ERP字段映射已确认，可以开始分析。"
        )
        self._append_log("ERP字段映射确认完成。")
        return True

    def _confirm_separate_opening_fields(self) -> bool:
        """独立期初文件存在多候选字段时复用ERP确认窗口。"""
        if self.erp_opening_mapping:
            return True
        opening_path = self.opening_inventory_file.get().strip()
        if not opening_path:
            return True
        try:
            detection = detect_erp_file(opening_path, table_kind="opening")
        except Exception:
            # 标准期初文件允许仅含物料名称；交由独立文件加载器继续识别。
            return True
        if detection.ambiguous_fields:
            dialog = ERPFieldConfirmationWindow(self.root, detection)
            self.root.wait_window(dialog.window)
            if dialog.result is None:
                return False
            self.erp_opening_mapping = dialog.result
            self._append_log(
                f"期初字段确认完成：文件={detection.file_path.name} | "
                f"Sheet={detection.sheet_name} | 映射={dialog.result}"
            )
        else:
            self.erp_opening_mapping = detection.resolved_mapping
        return True


class ERPFieldConfirmationWindow:
    """展示ERP检测结果，并要求用户确认所有多候选字段。"""

    FIELD_LABELS = {
        "date": "日期",
        "warehouse": "仓库",
        "material": "物料",
        "material_name": "物料名称",
        "quantity": "数量",
        "direction": "方向",
        "category_large": "大类",
        "category_middle": "中类",
        "category_small": "小类",
        "batch": "批次",
        "inventory_date": "库存日期",
    }

    def __init__(self, parent: tk.Tk, detection: ERPDetectionResult) -> None:
        self.detection = detection
        self.result: dict[str, str] | None = None
        self.variables: dict[str, tk.StringVar] = {}
        self.window = tk.Toplevel(parent)
        self.window.title("ERP字段确认")
        self.window.transient(parent)
        self.window.grab_set()
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"文件：{detection.file_path.name}\n"
            f"Sheet：{detection.sheet_name}",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        row_number = 1
        for field, candidates in detection.field_candidates.items():
            if not candidates:
                continue
            ttk.Label(
                frame, text=self.FIELD_LABELS.get(field, field)
            ).grid(row=row_number, column=0, padx=(0, 12), pady=4, sticky="e")
            initial = detection.resolved_mapping.get(field, candidates[0])
            variable = tk.StringVar(value=initial)
            self.variables[field] = variable
            box = ttk.Combobox(
                frame,
                textvariable=variable,
                values=list(candidates),
                state="readonly",
                width=32,
            )
            box.grid(row=row_number, column=1, pady=4, sticky="ew")
            row_number += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row_number, column=0, columnspan=2, pady=(14, 0))
        ttk.Button(
            buttons, text="确认导入", command=self._confirm
        ).pack(side="left", padx=5)
        ttk.Button(
            buttons, text="取消", command=self.window.destroy
        ).pack(side="left", padx=5)
        frame.columnconfigure(1, weight=1)

    def _confirm(self) -> None:
        self.result = {
            field: variable.get()
            for field, variable in self.variables.items()
            if variable.get()
        }
        self.window.destroy()


class RunHistoryWindow:
    """展示历史运行记录，双击打开报告所在目录。"""

    def __init__(self, parent: tk.Tk) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("历史运行记录")
        self.window.geometry("1080x520")
        columns = (
            "日期",
            "期间",
            "仓库",
            "费率规则",
            "仓储费",
            "报告路径",
            "运行状态",
        )
        self.tree = ttk.Treeview(
            self.window, columns=columns, show="headings"
        )
        for column in columns:
            self.tree.heading(column, text=column)
            width = 110
            if column in {"费率规则", "报告路径"}:
                width = 240
            self.tree.column(column, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=12, pady=12)
        self.tree.bind("<Double-Button-1>", self._open_directory)
        for index, record in enumerate(reversed(load_run_history())):
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    record.get("run_time", ""),
                    record.get("period", ""),
                    record.get("warehouse", ""),
                    record.get("rate", ""),
                    (
                        f"{record.get('fee'):,.2f}"
                        if isinstance(record.get("fee"), (int, float))
                        else ""
                    ),
                    record.get("output_path", ""),
                    record.get("status", ""),
                ),
            )

    def _open_directory(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        values = self.tree.item(selection[0], "values")
        report_path = Path(values[5])
        directory = report_path.parent
        if not directory.is_dir():
            messagebox.showwarning(
                "目录不存在", f"报告目录不存在：{directory}", parent=self.window
            )
            return
        try:
            os.startfile(directory)
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.window)


class WarehouseSelectionWindow:
    """显示自动识别仓库并由用户确认本次运行范围。"""

    def __init__(self, parent: tk.Tk, warehouses: list[str]) -> None:
        self.result: list[str] | None = None
        self.variables: dict[str, tk.BooleanVar] = {}
        self.window = tk.Toplevel(parent)
        self.window.title("确认多仓运行")
        self.window.transient(parent)
        self.window.grab_set()
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"发现仓库数量：{len(warehouses)}",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 10))
        for warehouse in warehouses:
            variable = tk.BooleanVar(value=True)
            self.variables[warehouse] = variable
            ttk.Checkbutton(
                frame, text=warehouse, variable=variable
            ).pack(anchor="w", pady=3)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(
            buttons, text="确认运行", command=self._confirm
        ).pack(side="left", padx=5)
        ttk.Button(
            buttons, text="取消", command=self.window.destroy
        ).pack(side="left", padx=5)

    def _confirm(self) -> None:
        selected = [
            warehouse
            for warehouse, variable in self.variables.items()
            if variable.get()
        ]
        if not selected:
            messagebox.showwarning(
                "未选择仓库",
                "请至少选择一个仓库。",
                parent=self.window,
            )
            return
        self.result = selected
        self.window.destroy()


class EnterpriseHistoryWindow:
    """查询SQLite企业运行历史并打开单仓报告目录。"""

    def __init__(self, parent: tk.Tk) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("企业运行历史")
        self.window.geometry("1080x560")
        self.date_filter = tk.StringVar()
        self.warehouse_filter = tk.StringVar()
        self.month_filter = tk.StringVar()
        self.status_filter = tk.StringVar()
        top = ttk.Frame(self.window, padding=10)
        top.pack(fill="x")
        for label, variable in (
            ("日期", self.date_filter),
            ("仓库", self.warehouse_filter),
            ("月份", self.month_filter),
            ("状态", self.status_filter),
        ):
            ttk.Label(top, text=label).pack(side="left", padx=(6, 3))
            ttk.Entry(top, textvariable=variable, width=13).pack(side="left")
        ttk.Button(top, text="查询", command=self._refresh).pack(
            side="left", padx=10
        )
        columns = ("日期", "仓库", "月份", "费用", "状态", "报告路径")
        self.tree = ttk.Treeview(
            self.window, columns=columns, show="headings"
        )
        for column in columns:
            self.tree.heading(column, text=column)
            self.tree.column(
                column,
                width=260 if column == "报告路径" else 130,
                anchor="w",
            )
        self.tree.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.tree.bind("<Double-Button-1>", self._open_directory)
        self._refresh()

    def _refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        records = query_history(
            date=self.date_filter.get().strip() or None,
            warehouse=self.warehouse_filter.get().strip() or None,
            month=self.month_filter.get().strip() or None,
            status=self.status_filter.get().strip() or None,
        )
        for index, record in enumerate(records):
            fee = record.get("仓储费")
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    record.get("运行时间", ""),
                    record.get("仓库名称", ""),
                    record.get("统计期间", ""),
                    f"{fee:,.2f}" if isinstance(fee, (int, float)) else "",
                    record.get("仓库状态") or record.get("运行状态", ""),
                    record.get("报告路径", ""),
                ),
            )

    def _open_directory(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        path = Path(self.tree.item(selection[0], "values")[5])
        if not path.parent.is_dir():
            messagebox.showwarning(
                "目录不存在", f"报告目录不存在：{path.parent}", parent=self.window
            )
            return
        try:
            os.startfile(path.parent)
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.window)


class RateManagerWindow:
    """系统费率库的搜索、查看、新增、修改、停用和导入窗口。"""

    def __init__(
        self,
        parent: tk.Tk,
        on_change,
        user: AuthUser,
        prefill: dict[str, object] | None = None,
    ) -> None:
        self.on_change = on_change
        self.user = user
        self.prefill = {
            key: str(value)
            for key, value in (prefill or {}).items()
            if value is not None
        }
        self.window = tk.Toplevel(parent)
        self.window.title("费率管理")
        self.window.geometry("940x600")
        self.search_text = tk.StringVar()

        top = ttk.Frame(self.window, padding=12)
        top.pack(fill="both", expand=True)
        ttk.Label(top, text="仓库名称/计费规则").grid(
            row=0, column=0, sticky="w"
        )
        entry = ttk.Entry(top, textvariable=self.search_text)
        entry.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(top, text="搜索", command=self._refresh).grid(row=0, column=2)

        self.rule_list = tk.Listbox(top, exportselection=False, width=38)
        self.rule_list.grid(row=1, column=0, columnspan=2, pady=10, sticky="nsew")
        self.rule_list.bind("<<ListboxSelect>>", self._show_details)
        self.rule_list.bind("<Double-Button-1>", self._show_details)

        columns = ("品类", "天数下限", "天数上限", "单价", "生效日期", "失效日期", "启用")
        self.details = ttk.Treeview(
            top, columns=columns, show="headings", selectmode="browse"
        )
        for column in columns:
            self.details.heading(column, text=column)
            self.details.column(column, width=90, anchor="center")
        self.details.grid(row=1, column=2, columnspan=4, pady=10, padx=(12, 0), sticky="nsew")
        self.details.bind("<Double-Button-1>", lambda _event: self._modify())

        buttons = ttk.Frame(top)
        buttons.grid(row=2, column=0, columnspan=6, sticky="ew")
        actions = [("修改" if user.role == ROLE_ADMIN else "提交变更申请", self._modify)]
        if has_permission(user, "manage_rates"):
            actions.extend(
                [
                    ("新增", self._add),
                    ("停用", self._disable),
                    ("导入费率配置", self._import),
                    ("审核申请", self._review_requests),
                ]
            )
        actions.extend(
            [("查看版本变化", self._show_versions), ("关闭", self.window.destroy)]
        )
        for text, command in actions:
            ttk.Button(buttons, text=text, command=command).pack(
                side="left", padx=5
            )
        top.columnconfigure(1, weight=1)
        top.columnconfigure(3, weight=1)
        top.rowconfigure(1, weight=1)
        self._refresh()
        if self.prefill:
            self.search_text.set(self.prefill.get("仓库名称", ""))
            self._refresh()
            if has_permission(user, "manage_rates"):
                self.window.after(100, self._add)

    def _selected_rule(self) -> str | None:
        selection = self.rule_list.curselection()
        return self.rule_list.get(selection[0]) if selection else None

    def _refresh(self) -> None:
        current = self._selected_rule()
        results = search_rate_rule(
            self.search_text.get(), include_disabled=True
        )
        self.rule_list.delete(0, "end")
        for item in results["选择项"]:
            self.rule_list.insert("end", item)
        if current:
            values = self.rule_list.get(0, "end")
            if current in values:
                self.rule_list.selection_set(values.index(current))
        self._show_details()
        self.on_change()

    def _show_details(self, _event=None) -> None:
        for item in self.details.get_children():
            self.details.delete(item)
        selection = self._selected_rule()
        if not selection:
            return
        data = load_rate_database()
        data = data[data["选择项"] == selection].sort_values(
            ["生效日期", "品类", "天数下限"], kind="stable"
        )
        for index, row in data.iterrows():
            self.details.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    row["品类"],
                    "" if str(row["天数下限"]) == "nan" else row["天数下限"],
                    "" if str(row["天数上限"]) == "nan" else row["天数上限"],
                    row["单价"],
                    row["生效日期"].strftime("%Y-%m-%d"),
                    "" if str(row["失效日期"]) == "NaT" else row["失效日期"].strftime("%Y-%m-%d"),
                    row["是否启用"],
                ),
            )

    def _ask(self, title: str, prompt: str, initial: str = "") -> str | None:
        return simpledialog.askstring(
            title, prompt, initialvalue=initial, parent=self.window
        )

    def _add(self) -> None:
        if self.prefill.get("仓库名称"):
            warehouse = self.prefill["仓库名称"]
            material = self.prefill.get("物料名称", "")
            category = self.prefill.get("品类", "") or material or "全部"
            rule = self._ask("创建费率规则", "计费规则")
            if rule is None:
                return
            price = self._ask("创建费率规则", "单价")
            if price is None:
                return
            effective = self._ask(
                "创建费率规则", "生效日期（YYYY-MM-DD）",
                self.prefill.get("生效日期", ""),
            )
            if effective is None:
                return
            fields = {
                "仓库编号": "AUTO",
                "仓库名称": warehouse,
                "计费规则": rule,
                "计价方式": "吨/天",
                "品类": category,
                "天数下限": 0,
                "天数上限": None,
                "单价": price,
                "生效日期": effective,
                "失效日期": None,
                "是否启用": "是",
                "备注": f"费率缺失快速创建；物料={material}；品类={category}",
            }
            try:
                add_rate_rule(fields)
                messagebox.showinfo(
                    "完成", "费率规则已作为新版本记录新增，未覆盖历史。",
                    parent=self.window,
                )
                self._refresh()
            except Exception as exc:
                messagebox.showerror("新增失败", str(exc), parent=self.window)
            return
        fields = {}
        prompts = [
            ("仓库编号", "仓库编号"),
            ("仓库名称", "仓库名称"),
            ("计费规则", "计费规则"),
            ("计价方式", "计价方式"),
            ("品类", "品类（全部或具体品类）"),
            ("天数下限", "天数下限"),
            ("天数上限", "天数上限（无上限可留空）"),
            ("单价", "单价"),
            ("生效日期", "生效日期（YYYY-MM-DD）"),
            ("失效日期", "失效日期（可留空）"),
            ("备注", "备注（可留空）"),
        ]
        for field, prompt in prompts:
            value = self._ask(
                "创建费率规则",
                prompt,
                self.prefill.get(field, ""),
            )
            if value is None:
                return
            fields[field] = value if value != "" else None
        fields["是否启用"] = "是"
        try:
            add_rate_rule(fields)
            messagebox.showinfo("完成", "费率规则已新增。", parent=self.window)
            self._refresh()
        except Exception as exc:
            messagebox.showerror("新增失败", str(exc), parent=self.window)

    def _modify(self) -> None:
        selection = self._selected_rule()
        detail = self.details.selection()
        if not selection or not detail:
            messagebox.showwarning("请选择", "请选择一条费率明细。", parent=self.window)
            return
        data = load_rate_database()
        row = data.loc[int(detail[0])]
        new_price = self._ask("修改费率", "单价", str(row["单价"]))
        if new_price is None:
            return
        new_rule = self._ask("修改费率", "计费规则", str(row["计费规则"]))
        if new_rule is None:
            return
        new_date = self._ask(
            "修改费率",
            "生效日期（YYYY-MM-DD）",
            row["生效日期"].strftime("%Y-%m-%d"),
        )
        if new_date is None:
            return
        new_note = self._ask("修改费率", "备注", str(row["备注"]))
        if new_note is None:
            return
        try:
            updates = {
                    "单价": float(new_price),
                    "计费规则": new_rule,
                    "生效日期": new_date,
                    "备注": new_note,
                }
            row_match = {
                    "品类": row["品类"],
                    "天数下限": row["天数下限"],
                    "天数上限": row["天数上限"],
                }
            if has_permission(self.user, "manage_rates"):
                before = row.to_dict()
                update_rate_rule(
                    selection, row["生效日期"], updates, row_match=row_match
                )
                record_audit(
                    user=self.user.username,
                    action="费率修改",
                    target=selection,
                    before=before,
                    after=updates,
                    result="已通过",
                )
                message = "费率明细已修改并写入审计记录。"
            else:
                request_id = submit_rate_change(
                    self.user,
                    selection=selection,
                    effective_date=row["生效日期"],
                    updates=updates,
                    row_match=row_match,
                )
                message = f"变更申请已提交，编号{request_id}，等待管理员审核。"
            messagebox.showinfo("完成", message, parent=self.window)
            self._refresh()
        except Exception as exc:
            messagebox.showerror("修改失败", str(exc), parent=self.window)

    def _show_versions(self) -> None:
        records = query_rate_version_changes()
        text = "\n".join(
            f"{item['日期']} | {item['仓库规则']} | {item['修改人']}"
            for item in records[:100]
        ) or "暂无费率版本变化记录。"
        messagebox.showinfo("费率版本变化", text, parent=self.window)

    def _review_requests(self) -> None:
        records = query_rate_requests(status="待审核")
        if not records:
            messagebox.showinfo("审核申请", "当前没有待审核申请。", parent=self.window)
            return
        request_id = simpledialog.askinteger(
            "审核申请",
            "待审核编号：\n"
            + "\n".join(
                f"{item['id']} | {item['申请人']} | {item['选择项']}"
                for item in records[:20]
            )
            + "\n\n请输入要批准的编号：",
            parent=self.window,
        )
        if request_id is None:
            return
        try:
            approve_rate_change(self.user, request_id)
            messagebox.showinfo("审核完成", "申请已批准并写入费率库。", parent=self.window)
            self._refresh()
        except Exception as exc:
            messagebox.showerror("审核失败", str(exc), parent=self.window)

    def _disable(self) -> None:
        selection = self._selected_rule()
        if not selection:
            messagebox.showwarning("请选择", "请选择费率规则。", parent=self.window)
            return
        version = self._ask(
            "停用费率", "需停用的版本日期（YYYY-MM-DD；留空停用全部版本）"
        )
        if version is None:
            return
        if not messagebox.askyesno(
            "确认停用", "停用后历史数据仍保留，确认继续？", parent=self.window
        ):
            return
        try:
            disable_rate_rule(selection, version or None)
            self._refresh()
        except Exception as exc:
            messagebox.showerror("停用失败", str(exc), parent=self.window)

    def _import(self) -> None:
        path = filedialog.askopenfilename(
            title="导入rate_config.xlsx",
            filetypes=[("Excel文件", "*.xlsx"), ("所有文件", "*.*")],
            parent=self.window,
        )
        if not path:
            return
        try:
            result = import_rate_config(path)
        except ValueError as exc:
            choice = messagebox.askyesnocancel(
                "发现已有规则",
                f"{exc}\n\n是：覆盖已有版本\n否：作为新版本导入\n取消：不导入",
                parent=self.window,
            )
            if choice is None:
                return
            try:
                if choice:
                    result = import_rate_config(path, conflict="overwrite")
                else:
                    version = self._ask(
                        "新增版本", "新版本生效日期（YYYY-MM-DD）"
                    )
                    if not version:
                        return
                    result = import_rate_config(
                        path,
                        conflict="new_version",
                        new_effective_date=version,
                    )
            except Exception as inner:
                messagebox.showerror("导入失败", str(inner), parent=self.window)
                return
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.window)
            return
        messagebox.showinfo("导入完成", str(result), parent=self.window)
        self._refresh()


class DashboardWindow:
    """只读展示历史数据库中的企业经营指标和图形。"""

    def __init__(self, parent: tk.Tk) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("经营驾驶舱")
        self.window.geometry("1120x760")
        data = load_dashboard_data()
        top = ttk.Frame(self.window, padding=12)
        top.pack(fill="both", expand=True)
        metrics = ttk.Frame(top)
        metrics.pack(fill="x")
        for index, (name, value) in enumerate(data.metrics.items()):
            text = f"{value:,.2f}" if isinstance(value, float) else str(value)
            ttk.Label(
                metrics,
                text=f"{name}\n{text}",
                padding=10,
                anchor="center",
                relief="solid",
            ).grid(row=0, column=index, padx=3, sticky="ew")
            metrics.columnconfigure(index, weight=1)
        notebook = ttk.Notebook(top)
        notebook.pack(fill="both", expand=True, pady=(12, 0))
        chart_tab = ttk.Frame(notebook)
        trend_tab = ttk.Frame(notebook)
        anomaly_tab = ttk.Frame(notebook)
        notebook.add(chart_tab, text="TOP排名")
        notebook.add(trend_tab, text="趋势分析")
        notebook.add(anomaly_tab, text="异常驾驶舱")

        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure

        figure = Figure(figsize=(10, 6), dpi=100)
        for position, (field, title) in enumerate(
            [("仓储费", "仓储费TOP10"), ("期末库存", "库存TOP10"), ("平均存放天数", "库龄TOP10")],
            1,
        ):
            axis = figure.add_subplot(1, 3, position)
            frame = data.warehouse_ranking.nlargest(10, field).sort_values(field)
            axis.barh(frame["仓库名称"], frame[field], color="#4472C4")
            axis.set_title(title)
        figure.tight_layout()
        FigureCanvasTkAgg(figure, master=chart_tab).get_tk_widget().pack(fill="both", expand=True)

        trend_figure = Figure(figsize=(10, 6), dpi=100)
        fee_axis = trend_figure.add_subplot(2, 1, 1)
        fee_axis.plot(data.trend["期间"], data.trend["仓储费"], marker="o")
        fee_axis.set_title("仓储费趋势")
        inventory_axis = trend_figure.add_subplot(2, 1, 2)
        inventory_axis.plot(data.trend["期间"], data.trend["月末库存"], marker="o", label="月末库存")
        inventory_axis.plot(data.trend["期间"], data.trend["平均库存"], marker="o", label="平均库存")
        inventory_axis.set_title("库存趋势")
        inventory_axis.legend()
        trend_figure.tight_layout()
        FigureCanvasTkAgg(trend_figure, master=trend_tab).get_tk_widget().pack(fill="both", expand=True)

        columns = ("异常分类", "时间", "仓库", "错误类型", "错误描述", "解决建议")
        tree = ttk.Treeview(anomaly_tab, columns=columns, show="headings")
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=150)
        for row in data.anomalies.to_dict("records"):
            tree.insert("", "end", values=[row.get(column, "") for column in columns])
        tree.pack(fill="both", expand=True)


class UserManagementWindow:
    def __init__(self, parent: tk.Tk, actor: AuthUser) -> None:
        self.actor = actor
        self.window = tk.Toplevel(parent)
        self.window.title("用户管理")
        self.window.geometry("700x460")
        self.tree = ttk.Treeview(
            self.window,
            columns=("id", "用户名", "角色", "状态", "创建时间", "最后登录时间"),
            show="headings",
        )
        for column in self.tree["columns"]:
            self.tree.heading(column, text=column)
        self.tree.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Button(self.window, text="新增用户", command=self._add).pack(pady=(0, 12))
        self._refresh()

    def _refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for item in list_users(self.actor):
            self.tree.insert("", "end", values=list(item.values()))

    def _add(self) -> None:
        username = simpledialog.askstring("新增用户", "用户名", parent=self.window)
        password = simpledialog.askstring("新增用户", "密码（至少8位）", show="*", parent=self.window)
        role = simpledialog.askstring("新增用户", "角色：Admin / Finance / Viewer", parent=self.window)
        if not all((username, password, role)):
            return
        try:
            created = create_user(username, password, role, actor=self.actor)
            record_audit(
                user=self.actor.username,
                action="用户管理",
                target=created.username,
                after={"角色": created.role},
            )
            self._refresh()
        except Exception as exc:
            messagebox.showerror("新增失败", str(exc), parent=self.window)


class AuditLogWindow:
    def __init__(self, parent: tk.Tk) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title("系统审计日志")
        self.window.geometry("980x560")
        columns = ("时间", "用户", "操作类型", "对象", "修改前", "修改后", "结果", "IP地址")
        tree = ttk.Treeview(self.window, columns=columns, show="headings")
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=120)
        for row in query_audit():
            tree.insert("", "end", values=[row.get(column, "") for column in columns])
        tree.pack(fill="both", expand=True, padx=12, pady=12)


class LoginWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        ensure_default_admin()
        root.title(version_label())
        root.geometry("440x300")
        frame = ttk.Frame(root, padding=32)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=version_label(), font=("Microsoft YaHei UI", 16, "bold")).pack(pady=(0, 24))
        self.username = tk.StringVar(value="admin")
        self.password = tk.StringVar()
        ttk.Label(frame, text="用户名").pack(anchor="w")
        ttk.Entry(frame, textvariable=self.username).pack(fill="x", pady=(4, 12))
        ttk.Label(frame, text="密码").pack(anchor="w")
        password_entry = ttk.Entry(frame, textvariable=self.password, show="*")
        password_entry.pack(fill="x", pady=(4, 20))
        password_entry.bind("<Return>", lambda _event: self._login())
        ttk.Button(frame, text="登录", command=self._login).pack(fill="x")
        self.frame = frame

    def _login(self) -> None:
        user = authenticate(self.username.get(), self.password.get())
        if user is None:
            record_audit(
                user=self.username.get() or "<空>",
                action="登录",
                result="失败",
            )
            messagebox.showerror("登录失败", "用户名、密码错误或用户已停用。")
            return
        record_audit(user=user.username, action="登录", result="成功")
        self.frame.destroy()
        WarehouseAnalysisApp(self.root, user)


def main() -> None:
    root = tk.Tk()
    LoginWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
