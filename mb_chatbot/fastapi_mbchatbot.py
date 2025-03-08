import json
import base64
import io
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import duckdb
import pandas as pd
import matplotlib.pyplot as plt
from openai import OpenAI
import os
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI()
openai_api_key = os.getenv("OPENAI_API_KEY", "default_secret")
client = OpenAI(api_key=openai_api_key)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "https://teams.microsoft.com"],  # Update with your frontend URLs
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods
    allow_headers=["*"],  # Allow all headers
)

# Load metadata
def load_metadata(file_path):
    with open(file_path, "r") as f:
        return json.load(f)

metadata_path = "mb_chatbot/data/metadata.json"
metadata = load_metadata(metadata_path)

def generate_schema_description(metadata):
    schema_description = []
    for table_name, table_info in metadata["tables"].items():
        schema_description.append(f"Table: {table_name}")
        schema_description.append(f"Description: {table_info['description']}")
        schema_description.append("Columns:")
        for column_name, column_description in table_info["columns"].items():
            schema_description.append(f"  - {column_name}: {column_description}")
        schema_description.append("\n")
    return "\n".join(schema_description)

schema_description = generate_schema_description(metadata)

def generate_sql_query(prompt, schema_description):
    full_prompt = f"""
        You are a helpful assistant that translates natural language questions into SQL queries. 
        If the prompt is casual conversation, return appriopriate response instead of trying to generate sql and also mention you're a data analyst agent.
        Backend is duckdb. For adding months use 'date_add(metric_month, INTERVAL 2 month)' .
        For past months use 'metric_month - interval 2 month'.
        use is_forecasted = 0 if not asked about future trends or forecasted values.
        Return only the SQL query. Dont have the 'sql' prefix. Always sort by metric_month before display as when applicable, ignore the order by if not needed.
        Use the sum of all wells when asked about overall data or trend.
        When asked about wells data, dont group by month, unless asked about a well and its trend.
        Use is_forecasted = 1 when asked about future trends or forecasted values for coming months. 
        avoid is_forecasted column for expense_data table.
        Use the schema below to understand the database structure:

        {schema_description}

        Generate only the SQL query, no explanation or extra text. Avoid sql prefix

        Question: {prompt}
    """
    chat_completion = client.chat.completions.create(
        messages=[{"role": "user", "content": full_prompt}],
        model="gpt-4o-mini",
    )
    # print(chat_completion.choices[0].message.content.strip().replace('`', ''), flush=True)
    return chat_completion.choices[0].message.content.strip().replace('`', '')

def execute_query(query, database_path="mb_chatbot/ddb/oildata.db"):
    conn = duckdb.connect(database=database_path, read_only=True)
    try:
        df = conn.execute(query).fetchdf()
    except Exception as e:
        return str(e)
    finally:
        conn.close()
    return df

def generate_descriptions(prompt, response_data):
    full_prompt = f"""
        You are an analytical assistant providing key insights. Generate a one-line summary and 3-4 observations based on the data.
        You will be provided with user query and the data(response) for the user query. 
        Also suggest follow up questions relevant to the user query based on the schema description.
        Avoid explicitly mentioning json.

        Format response as JSON:
        {{
            "summary": "Brief summary here",
            "observations": ["Observation 1", "Observation 2"],
            "follow_ups": ["Follow-up question 1", "Follow-up question 2"]
        }}

        User query: {prompt}

        Data(response): {response_data.values}

        Schema description: {schema_description}

        """
    chat_completion = client.chat.completions.create(
        messages=[
            {"role": "user", "content": full_prompt}
        ],
        model="gpt-4o-mini",
        temperature=0.2,
    )
    response_data =  chat_completion.choices[0].message.content.strip().replace('`','')
    if response_data.startswith('json'):
        response_data = response_data.replace('json','')
    # print(response_data)

    # print(chat_completion.choices[0].message.content.strip())
    return json.loads(response_data)


def generate_chart_code(prompt, response_data):
    full_prompt = f"""
        You are a data visualization expert for the oil and gas sector.
        You will be provided with user query and the corresponding data(response).
        Create a compelling visual based on the data.
        Send only the python code without the extra text.
        Dont generate instructions to run python code.
        Make sure the x-axis and y-axis has the proper text like whether the value is in thousands or millions.
        Dont use plt.show(). Just return the plot. Generate the plot purely in-memory.
        User query: {prompt}

        Data(response): {response_data.values}        
    """
    chat_completion = client.chat.completions.create(
        messages=[
            {"role": "user", "content": full_prompt}
        ],
        model="gpt-4o-mini",
        temperature=0.2,
    )
    # print(chat_completion.choices[0].message.content.strip())
    return chat_completion.choices[0].message.content.strip()

def execute_chart_code(chart_code, df):
    try:
        exec_globals = {"df": df, "plt": plt}
        exec(chart_code, exec_globals)
        fig = exec_globals["plt"].gcf()  # Get the current figure

        # Convert figure to Base64 image
        img_io = io.BytesIO()
        fig.savefig(img_io, format="png", bbox_inches="tight")
        img_io.seek(0)
        base64_img = base64.b64encode(img_io.getvalue()).decode("utf-8")
        plt.close(fig)
        return f"data:image/png;base64,{base64_img}"
    except Exception as e:
        st.error(f"Error in generated code execution: {e}")
        return None
    
# def generate_chart(df):
#     fig, ax = plt.subplots(figsize=(6, 4))
#     ax.plot(df["date"], df["value"], marker="o", linestyle="-", color="blue")
#     ax.set_title("Revenue Trend in Millions")
#     ax.set_xlabel("Date")
#     ax.set_ylabel("Revenue (in Millions)")
#     ax.grid(True)

#     # Save image as base64
#     img_buf = io.BytesIO()
#     plt.savefig(img_buf, format="png")
#     img_buf.seek(0)
#     img_base64 = base64.b64encode(img_buf.getvalue()).decode()
#     plt.close()

#     return f"data:image/png;base64,{img_base64}"

@app.get("/ui", response_class=HTMLResponse)
async def serve_ui():
    with open("mb_chatbot/chatui.html", "r") as f:
        return f.read()

@app.get("/chatbot", response_class=HTMLResponse)
async def chatbot_endpoint(email: str = Query(...), query: str = Query(...)):
    try:
        sql_query = generate_sql_query(query, schema_description)
        result = execute_query(sql_query)

        if isinstance(result, str):
            return f"<h2>Error:</h2><p>{result}</p>"

        response_data = generate_descriptions(query, result)
        # chart_code = generate_chart_code(user_query, result).replace('```python', '').replace('```', '').strip()
        chart_code = generate_chart_code(query,result).replace('```python', '').replace('```', '').strip()
        chart_base64 = execute_chart_code(chart_code, result)

        summary = response_data["summary"]
        observations = response_data["observations"]
        follow_ups = response_data["follow_ups"]

        # Follow-up questions
        buttons_html = "".join([f'<button>{q}</button><br>' for q in follow_ups])
        observations_html = "".join([f'<li>{o}</li><br>' for o in observations])

        return f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; padding: 20px; background-color: #121212; color: #ffffff; }}
                h2 {{ color: #00aaff; }}
                .summary {{ background: #1e1e1e; padding: 15px; border-radius: 8px; margin-top: 10px; }}
                .related {{ margin-top: 20px; }}
                button {{ background: #0078D4; color: white; padding: 10px; border: none; cursor: pointer; margin: 5px; }}
            </style>
        </head>
        <body>
            <h2>📊 Revenue Insights for {email}</h2>
            <img src="{chart_base64}" alt="Revenue Trend" width="500px">
            <div class="summary">
                <h3>💡 Summary:</h3> <p>{summary}</p>
                <ul>
                    {observations_html}
                </ul>
            </div>
            <div class="related">
                <h3>🔍 Related:</h3>
                {buttons_html}
            </div>
        </body>
        </html>
        """
    except Exception as e:
        return f"<h2>Error</h2><p>{str(e)}</p>"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="debug")
