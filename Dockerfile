FROM python:3.12-slim

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r Business_Workbench/intel_app/requirements.txt

ENV INTEL_APP_BOOTSTRAP_OWNER_USERNAME=owner
ENV INTEL_APP_BOOTSTRAP_OWNER_KEY=owner-change-me
ENV INTEL_APP_BOOTSTRAP_COLLAB_USERNAME=collaborator
ENV INTEL_APP_BOOTSTRAP_COLLAB_KEY=collab-change-me
ENV INTEL_APP_ASSISTANT_KEY=assistant-ingest-change-me
ENV INTEL_APP_SESSION_SECRET=intel-app-session-secret-change-me

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "Business_Workbench.intel_app.app:app", "--host", "0.0.0.0", "--port", "8080"]
