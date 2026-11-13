import gradio as gr
import sys
import threading
import queue
from io import TextIOBase
from inference import inference_patch
from starter_prep import make_starter
import datetime
import subprocess
import os
import tempfile
import traceback
import re

# Predefined valid combinations set
with open('prompts.txt', 'r') as f:
    prompts = f.readlines()
valid_combinations = set()
for prompt in prompts:
    prompt = prompt.strip()
    parts = prompt.split('_')
    valid_combinations.add((parts[0], parts[1], parts[2]))

# Generate available options
periods = sorted({p for p, _, _ in valid_combinations})
composers = sorted({c for _, c, _ in valid_combinations})
instruments = sorted({i for _, _, i in valid_combinations})

# Dynamic component updates
def update_components(period, composer):
    if not period:
        return [
            gr.Dropdown(choices=[], value=None, interactive=False),
            gr.Dropdown(choices=[], value=None, interactive=False)
        ]
    
    valid_composers = sorted({c for p, c, _ in valid_combinations if p == period})
    valid_instruments = sorted({i for p, c, i in valid_combinations if p == period and c == composer}) if composer else []
    
    return [
        gr.Dropdown(
            choices=valid_composers,
            value=composer if composer in valid_composers else None,
            interactive=True  
        ),
        gr.Dropdown(
            choices=valid_instruments,
            value=None,
            interactive=bool(valid_instruments)  
        )
    ]


class RealtimeStream(TextIOBase):
    def __init__(self, queue):
        self.queue = queue
    
    def write(self, text):
        self.queue.put(text)
        return len(text)


def save_and_convert(abc_content, period, composer, instrumentation, generation_mode, starter_file=None):
    if not abc_content:
        raise gr.Error("Generate a composition before saving")
    if generation_mode == "Generate from prompt":
        if (period, composer, instrumentation) not in valid_combinations:
            raise gr.Error("Please complete a valid generation first before saving")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if generation_mode == "Generate from prompt":
        prompt_str = re.sub(r'[^A-Za-z0-9._-]+', '_',
                    f"{period}_{composer}_{instrumentation}")
    elif starter_file:
        stem = os.path.splitext(os.path.basename(starter_file))[0]
        stem = re.sub(r'^\d{8}_\d{6}_', '', stem)       # drop a leading timestamp from re-uploaded outputs
        stem = re.sub(r'(_continued)+$', '', stem)      # avoid "_continued_continued"
        prompt_str = re.sub(r'[^A-Za-z0-9._-]+', '_', stem) + "_continued"
    else:
        prompt_str = "Continuation"
    filename_base = f"{timestamp}_{prompt_str}"

    output_dir = "output"
    os.makedirs(output_dir, exist_ok=True)
    
    abc_filename = os.path.join(output_dir, f"{filename_base}.abc")
    with open(abc_filename, "w", encoding="utf-8") as f:
        f.write(abc_content)

    xml_filename = os.path.join(output_dir, f"{filename_base}.xml")
    try:
        subprocess.run(
            ["python", "abc2xml.py", '-o', output_dir, abc_filename, ],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        error_msg = f"Conversion failed: {e.stderr}" if e.stderr else "Unknown error"
        raise gr.Error(f"ABC to XML conversion failed: {error_msg}. Please try to generate another composition.")
    
    return f"Saved successfully: {abc_filename} -> {xml_filename}"


def load_starter(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".abc":
         with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    elif ext in {".xml", ".musicxml", ".mxl"}:
        converter = os.path.join(os.path.dirname(__file__), "..", "data", "xml2abc.py")
        with tempfile.TemporaryDirectory(prefix="notagen-xml-") as tmp:
            subprocess.run([sys.executable, converter, "-d", "8", "-c", "6", "-x", "-o", tmp, path],
                           check=True, capture_output=True, text=True)
            out = os.path.join(tmp, os.path.splitext(os.path.basename(path))[0] + ".abc")
            with open(out, "r", encoding="utf-8") as f:
                text = f.read()
    else:
        raise gr.Error("Please upload an .abc, .xml, .musicxml, or .mxl file")
    return make_starter(text)


def generate_music(generation_mode, period, composer, instrumentation, starter_abc_file, continuation_bars):
    starter_abc = None
    if (period, composer, instrumentation) not in valid_combinations:
        raise gr.Error("Invalid prompt combination! Please re-select the period, composer, and instrumentation")
    if generation_mode != "Generate from prompt":
        if not starter_abc_file:
            raise gr.Error("Upload an unfinished ABC or MusicXML file to continue")
        try:
            starter_abc = load_starter(starter_abc_file)
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.strip() if e.stderr else "The converter returned an error"
            raise gr.Error(f"MusicXML to ABC conversion failed: {error_msg}") from e
        except (OSError, UnicodeDecodeError, ValueError) as e:
            raise gr.Error(f"Could not read or convert the uploaded score: {e}") from e

   
    output_queue = queue.Queue()
    original_stdout = sys.stdout
    sys.stdout = RealtimeStream(output_queue)

    result_container = []
    error_container = []
    def run_inference():
        try:
            result_container.append(
                inference_patch(
                    period,
                    composer,
                    instrumentation,
                    starter_abc=starter_abc,
                    continuation_bars=continuation_bars,
                )
            )
        except Exception as e:
            traceback.print_exc(file=sys.__stderr__)
            error_container.append(e)
        finally:
            sys.stdout = original_stdout
    
    thread = threading.Thread(target=run_inference)
    thread.start()
    
    process_output = ""
    while thread.is_alive():
        try:
            text = output_queue.get(timeout=0.1)
            process_output += text
            yield process_output, None  
        except queue.Empty:
            continue
    
    while not output_queue.empty():
        text = output_queue.get()
        process_output += text
        yield process_output, None

    if error_container:
        raise gr.Error(f"Generation failed: {error_container[0]}")

    final_result = result_container[0] if result_container else ""
    yield process_output, final_result


def update_generation_mode(generation_mode):
    prompt_mode = generation_mode == "Generate from prompt"
    return (
        gr.update(visible=True),
        gr.update(visible=True),
        gr.update(visible=True),
        gr.update(visible=not prompt_mode),
        gr.update(visible=not prompt_mode),
    )

with gr.Blocks() as demo:
    gr.Markdown("## NotaGen")
    
    with gr.Row():
        # 左侧栏
        with gr.Column():
            generation_mode = gr.Radio(
                choices=["Generate from prompt", "Continue from a score file"],
                value="Generate from prompt",
                label="Generation mode",
            )
            period_dd = gr.Dropdown(
                choices=periods,
                value=None, 
                label="Period",
                interactive=True
            )
            composer_dd = gr.Dropdown(
                choices=[],
                value=None,
                label="Composer",
                interactive=False
            )
            instrument_dd = gr.Dropdown(
                choices=[],
                value=None,
                label="Instrumentation",
                interactive=False
            )
            starter_abc_file = gr.File(
                label="Unfinished ABC or MusicXML file (experimental)",
                file_types=[".abc", ".xml", ".musicxml", ".mxl"],
                type="filepath",
                visible=False,
            )
            continuation_bars = gr.Slider(
                minimum=1,
                maximum=256,
                step=1,
                value=64,
                label="Continuation target (bars, approximate)",
                visible=False,
            )
            
            generate_btn = gr.Button("Generate!", variant="primary")
            
            process_output = gr.Textbox(
                label="Generation process",
                interactive=False,
                lines=15,
                max_lines=15,
                placeholder="Generation progress will be shown here...",
                elem_classes="process-output"
            )

        # 右侧栏
        with gr.Column():
            final_output = gr.Textbox(
                label="Post-processed ABC notation scores",
                interactive=True,
                lines=23,
                placeholder="Post-processed ABC scores will be shown here...",
                elem_classes="final-output"
            )
            
            with gr.Row():
                save_btn = gr.Button("💾 Save as ABC & XML files", variant="secondary")
            
            save_status = gr.Textbox(
                label="Save Status",
                interactive=False,
                visible=True,
                max_lines=2
            )
    
    period_dd.change(
        update_components,
        inputs=[period_dd, composer_dd],
        outputs=[composer_dd, instrument_dd]
    )
    composer_dd.change(
        update_components,
        inputs=[period_dd, composer_dd],
        outputs=[composer_dd, instrument_dd]
    )

    generation_mode.change(
        update_generation_mode,
        inputs=[generation_mode],
        outputs=[period_dd, composer_dd, instrument_dd, starter_abc_file, continuation_bars]
    )
    
    generate_btn.click(
        generate_music,
        inputs=[
            generation_mode,
            period_dd,
            composer_dd,
            instrument_dd,
            starter_abc_file,
            continuation_bars,
        ],
        outputs=[process_output, final_output]
    )
    
    save_btn.click(
        save_and_convert,
        inputs=[final_output, period_dd, composer_dd, instrument_dd, generation_mode, starter_abc_file],
        outputs=[save_status]
    )


css = """
.process-output {
    background-color: #f0f0f0;
    font-family: monospace;
    padding: 10px;
    border-radius: 5px;
}
.final-output {
    background-color: #ffffff;
    font-family: sans-serif;
    padding: 10px;
    border-radius: 5px;
}

.process-output textarea {
    max-height: 500px !important;
    overflow-y: auto !important;
    white-space: pre-wrap;
}

"""
css += """
button#💾-save-convert:hover {
    background-color: #ffe6e6;
}
"""

demo.css = css

if __name__ == "__main__":

    demo.launch(
        server_name="0.0.0.0",
        server_port=7861,
        share=True
    )