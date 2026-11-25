# JupyterLab user image for the lab: Python only, no JVM.
#   pyspark-client = the pure-Python Spark Connect client (Spark 4.x). It talks gRPC to a remote
#   Spark Connect server and cannot start a local Spark driver (there is no Java in this image).
#   spiffe (py-spiffe) = Workload API client, so zt_auth can fetch the notebook's JWT-SVID
#   (spiffe://zt.lab/ns/jupyter/sa/jupyter-user) for the downscoping exchange (demo 04, hop 2).
#   Usage:  import zt_auth; tok = zt_auth.spark_token()
#           spark = SparkSession.builder.remote(f"sc://spark-connect...:15002/;token={tok}").getOrCreate()
FROM quay.io/jupyter/minimal-notebook:python-3.13.18

# pyspark-client matches the Spark Connect server (4.2.1: Delta and the UC connector have no 4.3 build yet).
# pandas<3: pyspark 4.x warns it does not fully support pandas 3 yet.
RUN pip install --no-cache-dir "pyspark-client==4.2.1" "pandas>=2.2,<3" "spiffe==0.4.0" \
 && fix-permissions "${CONDA_DIR}" \
 && fix-permissions "/home/${NB_USER}"

# Guard: no JVM in the image, and the Connect builder API and py-spiffe are importable.
RUN ! command -v java \
 && python -c "import pyspark; from pyspark.sql import SparkSession; assert hasattr(SparkSession.builder, 'remote'); print('pyspark-client', pyspark.__version__, '(no JVM)')" \
 && python -c "from spiffe import WorkloadApiClient; print('py-spiffe ok')"
