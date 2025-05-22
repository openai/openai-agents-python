import sys
from pathlib import Path
# Ensure src directory is in path for imports if running app.py directly
sys.path.append(str(Path(__file__).resolve().parents[2]))

from flask import Flask, render_template, abort
from src.agents.tracing.setup import GLOBAL_TRACE_PROVIDER
from src.agents.extensions.visualization import generate_graph_from_trace
import graphviz # For rendering DOT to SVG

app = Flask(__name__, template_folder='templates')

# In a real scenario, you might run your agent code first,
# then start this Flask app to view the traces.
# For this subtask, we'll assume GLOBAL_TRACE_PROVIDER is populated
# by some means before accessing the web page (e.g. by running an agent script
# that uses the same GLOBAL_TRACE_PROVIDER instance).

@app.route('/')
def home():
    # Basic home page, later can list traces
    trace_ids = list(GLOBAL_TRACE_PROVIDER.get_collected_traces().keys())
    return f'''
    <h1>Available Traces</h1>
    <ul>
        {''.join(f'<li><a href="/trace/{tid}">{tid}</a></li>' for tid in trace_ids)}
    </ul>
    If no traces are listed, run an agent script that uses tracing first.
    '''

@app.route('/trace/<trace_id>')
def view_trace_route(trace_id: str):
    trace_data = GLOBAL_TRACE_PROVIDER.get_collected_traces().get(trace_id)
    if not trace_data:
        return abort(404, description="Trace not found.")

    try:
        dot_string = generate_graph_from_trace(trace_data)
        # Render DOT string to SVG using graphviz library
        # The pipe() method returns bytes, so decode to utf-8 for HTML embedding
        svg_output = graphviz.Source(dot_string, format="svg").pipe().decode('utf-8')
    except Exception as e:
        app.logger.error(f"Error generating graph for trace {trace_id}: {e}")
        return abort(500, description=f"Error generating graph: {e}")

    return render_template('view_trace.html',
                           trace_id=trace_id,
                           trace_name=trace_data.name,
                           graph_svg=svg_output,
                           num_spans=len(trace_data.spans))

if __name__ == '__main__':
    # Note: For development, it's better to use `flask run` command.
    # This basic `app.run` is for simplicity in the subtask.
    # Ensure an agent script has run and populated GLOBAL_TRACE_PROVIDER before starting.
    print("Flask server starting. Run an agent script in another terminal/process first to populate traces.")
    print("To see traces, navigate to http://127.0.0.1:5001/ after traces are populated.")
    print("Then, navigate to http://127.0.0.1:5001/trace/<trace_id>")
    app.run(debug=True, port=5001) # Using a different port in case 5000 is common
