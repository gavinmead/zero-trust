# JupyterHub hub image for the lab: the chart's hub image plus py-spiffe, so the hub can fetch its
# own JWT-SVID (spiffe://zt.lab/ns/jupyter/sa/jupyter-session) from the SPIRE Workload API and use
# it as the client assertion for the session-token exchange (demo 04, hop 1).
FROM quay.io/jupyterhub/k8s-hub:4.4.5

USER root
RUN pip install --no-cache-dir --root-user-action=ignore "spiffe==0.4.0"
USER jovyan

RUN python3 -c "from spiffe import WorkloadApiClient; print('py-spiffe ok')"
