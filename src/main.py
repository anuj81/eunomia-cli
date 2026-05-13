import json
import typer
import httpx
from httpx_sse import connect_sse
from rich.console import Console
from rich.table import Table

app = typer.Typer()
console = Console()

@app.command()
def ask(query: str, token: str = typer.Option("finance-token", help="User Auth Token")):
    """
    Ask a question to Eunomia Middleware.
    """
    url = "http://localhost:8000/v1/execute_nlq"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"query": query}

    console.print(f"[bold blue]Querying:[/bold blue] {query}")
    
    try:
        with httpx.Client() as client:
            with connect_sse(client, "POST", url, headers=headers, json=payload) as event_source:
                for sse in event_source.iter_sse():
                    if sse.event == "complete":
                        data = json.loads(sse.data)
                        console.print("\n[bold green]Execution Complete![/bold green]")
                        from rich.panel import Panel
                        from rich.syntax import Syntax
                        sql_syntax = Syntax(data.get('executed_sql', 'No SQL returned'), "sql", theme="monokai", line_numbers=False)
                        console.print(Panel(sql_syntax, title="[bold blue]Generated SQL[/bold blue]", border_style="blue"))
                        
                        # Render table
                        results = data['results']
                        if results:
                            table = Table(show_header=True, header_style="bold magenta")
                            for key in results[0].keys():
                                table.add_column(key)
                            for row in results:
                                table.add_row(*[str(val) for val in row.values()])
                            console.print(table)
                        else:
                            console.print("No results returned.")
                        break
                    else:
                        # Status update
                        try:
                            data = json.loads(sse.data)
                            status = data.get("status", "")
                            console.print(f"[cyan]>[/cyan] {status}")
                        except json.JSONDecodeError:
                            console.print(f"[red]Raw Event:[/red] {sse.data}")
    except httpx.ConnectError:
        console.print("[bold red]Error:[/bold red] Could not connect to middleware. Is the FastAPI server running on port 8000?")

if __name__ == "__main__":
    app()
