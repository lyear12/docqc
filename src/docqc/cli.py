"""The ``docqc`` command line.

The CLI is the first milestone's user interface, and deliberately the only one
for now: until the checking logic is trustworthy, a browser UI would just be a
prettier way to display the same unverified numbers.

Three commands:

    docqc check <file>     run checkers, print a report
    docqc list             show registered checkers
    docqc backends         show which renderers this machine has

Exit codes follow the convention CI expects: ``0`` pass, ``1`` findings at
ERROR, ``2`` usage or infrastructure failure. That lets a GitHub Action gate on
a document the same way it gates on tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table as RichTable
from rich.text import Text

from . import __version__
from .models import Report, Severity, Status
from .registry import registered
from .runner import check as run_check

app = typer.Typer(
    name="docqc",
    help="文档质检工具：排版、公式、结构。",
    add_completion=False,
    no_args_is_help=True,
)

console = Console()
error_console = Console(stderr=True)

#: Exit codes, kept explicit because CI depends on their meaning.
EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2

#: Severity -> display style.
_SEVERITY_STYLE = {
    Severity.ERROR: "bold red",
    Severity.WARNING: "yellow",
    Severity.INFO: "cyan",
}

#: Severity -> short Chinese label.
_SEVERITY_LABEL = {
    Severity.ERROR: "错误",
    Severity.WARNING: "警告",
    Severity.INFO: "提示",
}

#: Status -> (label, style).
_STATUS_STYLE = {
    Status.PASS: ("通过", "bold green"),
    Status.WARN: ("有警告", "yellow"),
    Status.FAIL: ("不通过", "bold red"),
    Status.ERROR: ("检查器出错", "bold red"),
    Status.SKIPPED: ("已跳过", "dim"),
}


def _version_callback(value: bool) -> None:
    """Print the version and exit.

    Args:
        value: True when ``--version`` was passed.
    """
    if value:
        console.print(f"docqc {__version__}")
        raise typer.Exit(code=EXIT_OK)


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="显示版本号并退出。",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """docqc — document quality checker.

    Args:
        version: Handled by the eager callback; unused here.
    """


@app.command()
def check(
    path: Annotated[Path, typer.Argument(help="要检查的 .docx 文件。")],
    only: Annotated[
        list[str] | None,
        typer.Option("--only", "-o", help="只运行指定的检查器（可重复）。"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="输出完整 JSON 报告（供下游程序消费）。"),
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-O", help="把 JSON 报告写入文件。"),
    ] = None,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", "-q", help="只输出总结，不逐条列出问题。"),
    ] = False,
    show_info: Annotated[
        bool,
        typer.Option("--info", help="同时列出 INFO 级别的提示。"),
    ] = False,
) -> None:
    """检查一个文档。

    Args:
        path: Document to inspect.
        only: Restrict to these checkers.
        as_json: Emit the raw JSON report instead of the rendered view.
        output: Also write the JSON report here.
        quiet: Print only the summary block.
        show_info: Include INFO findings in the listing.
    """
    if not path.exists():
        error_console.print(f"[red]找不到文件：[/red]{path}")
        raise typer.Exit(code=EXIT_USAGE)

    with console.status("[dim]检查中…[/dim]", spinner="dots") as status:
        def progress(name: str, index: int, total: int) -> None:
            status.update(f"[dim]检查中… ({index}/{total}) {name}[/dim]")

        try:
            report = run_check(path, names=only, progress=progress)
        except KeyError as exc:
            error_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=EXIT_USAGE) from exc
        except (FileNotFoundError, ValueError) as exc:
            error_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=EXIT_USAGE) from exc

    if output is not None:
        output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        if not as_json:
            console.print(f"[dim]JSON 报告已写入 {output}[/dim]")

    if as_json:
        # Machine-readable mode: nothing but the JSON on stdout, so a caller can
        # pipe it without stripping decoration.
        sys.stdout.write(report.model_dump_json(indent=2))
        sys.stdout.write("\n")
    else:
        _render_report(report, quiet=quiet, show_info=show_info)

    raise typer.Exit(code=_exit_code(report))


@app.command(name="list")
def list_checkers() -> None:
    """列出所有已注册的检查器。"""
    table = RichTable(title="已注册的检查器", show_lines=False)
    table.add_column("名称", style="bold")
    table.add_column("顺序", justify="right", style="dim")
    table.add_column("说明")

    entries = registered()
    if not entries:
        console.print("[yellow]没有已注册的检查器。[/yellow]")
        raise typer.Exit(code=EXIT_USAGE)

    for entry in entries:
        table.add_row(entry.name, str(entry.order), entry.description)
    console.print(table)


@app.command()
def backends() -> None:
    """显示本机可用的渲染后端。"""
    from .render import detect_backends

    table = RichTable(title="渲染后端", show_lines=False)
    table.add_column("后端", style="bold")
    table.add_column("可用")
    table.add_column("说明")

    for backend in detect_backends():
        mark = Text("是", style="green") if backend.available else Text("否", style="red")
        table.add_row(backend.name, mark, backend.detail)
    console.print(table)
    console.print(
        "\n[dim]版面检查需要至少一个可用后端；其余检查器不需要渲染。[/dim]"
    )


# -- rendering ---------------------------------------------------------------


def _exit_code(report: Report) -> int:
    """Map a report to a process exit code.

    Args:
        report: The finished report.

    Returns:
        ``1`` when the run has ERROR findings or an errored checker, else ``0``.
    """
    if report.summary.status in {Status.FAIL, Status.ERROR}:
        return EXIT_FINDINGS
    return EXIT_OK


def _render_report(report: Report, *, quiet: bool, show_info: bool) -> None:
    """Print the human-readable report.

    Args:
        report: The finished report.
        quiet: Skip the per-finding listing.
        show_info: Include INFO findings.
    """
    document = report.document
    console.print()
    console.print(
        Panel(
            f"[bold]{document.name}[/bold]\n"
            f"[dim]{document.path}[/dim]\n"
            f"[dim]{document.size_bytes:,} B · sha256 {document.sha256[:12]}… · "
            f"{document.paragraph_count or 0} 段 · {document.table_count or 0} 表[/dim]",
            title="文档",
            border_style="blue",
        )
    )

    _render_checkers(report)

    if not quiet:
        _render_findings(report, show_info=show_info)

    _render_summary(report)


def _render_checkers(report: Report) -> None:
    """Print the per-checker status table.

    Args:
        report: The finished report.
    """
    table = RichTable(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("检查器", style="bold")
    table.add_column("状态")
    table.add_column("错误", justify="right")
    table.add_column("警告", justify="right")
    table.add_column("耗时", justify="right", style="dim")

    for result in report.results:
        label, style = _STATUS_STYLE[result.status]
        counts = result.counts
        table.add_row(
            result.checker,
            Text(label, style=style),
            str(counts[Severity.ERROR.value]) if counts[Severity.ERROR.value] else "[dim]·[/dim]",
            str(counts[Severity.WARNING.value])
            if counts[Severity.WARNING.value]
            else "[dim]·[/dim]",
            f"{result.duration_ms} ms",
        )
    console.print(table)

    for result in report.results:
        if result.status is Status.ERROR and result.error:
            console.print(
                Panel(
                    result.error.strip().splitlines()[0][:400],
                    title=f"[red]{result.checker} 出错[/red]",
                    border_style="red",
                )
            )


def _render_findings(report: Report, *, show_info: bool) -> None:
    """Print findings, most severe first.

    Args:
        report: The finished report.
        show_info: Include INFO findings.
    """
    wanted = {Severity.ERROR, Severity.WARNING}
    if show_info:
        wanted.add(Severity.INFO)

    findings = [f for f in report.findings() if f.severity in wanted]
    if not findings:
        return

    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    findings.sort(key=lambda f: order[f.severity])

    console.print()
    for finding in findings:
        label = _SEVERITY_LABEL[finding.severity]
        style = _SEVERITY_STYLE[finding.severity]
        where = finding.location.describe()
        console.print(f"  [{style}]{label}[/{style}] [dim]{where}[/dim] {finding.message}")
        console.print(f"        [dim]{finding.checker} · {finding.code}[/dim]")
        if finding.hint:
            console.print(f"        [dim]→ {finding.hint}[/dim]")
    console.print()


def _render_summary(report: Report) -> None:
    """Print the closing summary line.

    Args:
        report: The finished report.
    """
    summary = report.summary
    label, style = _STATUS_STYLE[summary.status]
    parts = [
        f"[{style}]{label}[/{style}]",
        f"[red]{summary.errors} 错误[/red]",
        f"[yellow]{summary.warnings} 警告[/yellow]",
    ]
    if summary.infos:
        parts.append(f"[cyan]{summary.infos} 提示[/cyan]")
    if summary.checkers_errored:
        parts.append(f"[red]{summary.checkers_errored} 个检查器出错[/red]")

    console.print(Panel(" · ".join(parts), border_style="dim"))
    console.print()


def main() -> None:
    """Entry point used by the console script."""
    app()


if __name__ == "__main__":  # pragma: no cover - manual invocation
    main()
