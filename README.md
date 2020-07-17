# azure-k8s-deploy
Deploy K8s in Azure

Python script calls the Azure API to create a kubernetes Cluster. Calls to create the following functions from the API call:

1. Get the Azure credentials to make API calls
2. Get the service profile
3. Resource client to initiate Azure Resource creation
4. Container client - to initiate container creation in kubernetes service
5. Create resource groups to logical grouping of resources created, in this case Kubernetes.
6. Create the cluster using the resource client, container client and in the resource group
7. Create the deployment object to define the K8s service, manifest to expose external IP for Loadbalancer.
8. Deploy the created object for k8s service.
9. Create the k8s cluster service.
10. Obtain cluster information using az cli
11. Get the cluster endpoint exposed from the external LB IP.
12. Wait for the cluster endpoint to be exposed - poll for endpoing

Goal of the entire script is to create a kubernetes cluster, expose the external IPs for connections, etc.
the rest of the script explains itself
