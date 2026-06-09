"""``leartech-agent topology`` — Mermaid diagram of the platform topology."""

from __future__ import annotations

import base64
import subprocess
import webbrowser

import click

from app.agent_cli.render import client_from_ctx, console, print_http_error


@click.command()
@click.option(
    '--render',
    type=click.Choice(['mermaid', 'png', 'browser', 'copy']),
    default='mermaid',
    help='Output format. mermaid=stdout, png=requires mmdc, browser=opens mermaid.live, copy=clipboard.',
)
@click.option('--output', '-o', default='/tmp/topo.png', help='Output path for --render png')
@click.option('--feedback', is_flag=True, help='Render only the feedback rings, not the full topology')
@click.pass_context
def topology(ctx: click.Context, render: str, output: str, feedback: bool) -> None:
    """Mermaid diagram of the platform topology."""
    path = '/topology/feedback' if feedback else '/topology'
    response = client_from_ctx(ctx.obj).get(path)
    if response.status_code != 200:
        print_http_error(response)
        return
    src: str = response.json()['mermaid']

    if render == 'mermaid':
        console.print(src)
    elif render == 'png':
        try:
            subprocess.run(['mmdc', '-i', '-', '-o', output], input=src.encode(), check=True)
            console.print(f'[green]Wrote {output}[/green]')
        except FileNotFoundError:
            console.print('[red]mmdc not installed.[/red] Try: npm install -g @mermaid-js/mermaid-cli')
        except subprocess.CalledProcessError as exc:
            console.print(f'[red]mmdc failed: {exc}[/red]')
    elif render == 'browser':
        encoded = base64.b64encode(src.encode()).decode()
        url = f'https://mermaid.live/edit#base64:{encoded}'
        console.print(f'Opening {url}')
        webbrowser.open(url)
    elif render == 'copy':
        try:
            subprocess.run(['pbcopy'], input=src.encode(), check=True)
            console.print('[green]Copied to clipboard.[/green]')
        except FileNotFoundError:
            console.print(
                '[red]pbcopy not available (this command is macOS-only). Pipe to your clipboard tool manually.[/red]'
            )
