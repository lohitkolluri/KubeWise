from fastapi import APIRouter

from app.models.api import ClusterStatusResponse
from app.services.kubernetes_monitor import kubernetes_monitor

router = APIRouter(prefix="/cluster", tags=["cluster"])


@router.get("/status", response_model=ClusterStatusResponse)
async def get_cluster_status():
    """
    Get a summary of the cluster status.

    Returns:
        Summary of cluster status
    """
    # Get cluster summary
    nodes_total, nodes_ready, pods_total, pods_running, deployments_total, deployments_ready, services_total = (
        await kubernetes_monitor.get_cluster_summary()
    )

    # Create response
    return ClusterStatusResponse(
        nodes_total=nodes_total,
        nodes_ready=nodes_ready,
        pods_total=pods_total,
        pods_running=pods_running,
        deployments_total=deployments_total,
        deployments_ready=deployments_ready,
        services_total=services_total
    )
