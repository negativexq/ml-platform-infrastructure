package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

type kubeClient struct {
	client    *http.Client
	endpoint  string
	tokenFile string
}

func (k *kubeClient) get(ctx context.Context, path string) (*http.Response, error) {
	token, err := os.ReadFile(k.tokenFile)
	if err != nil {
		return nil, err
	}
	r, err := http.NewRequestWithContext(ctx, http.MethodGet, k.endpoint+path, nil)
	if err != nil {
		return nil, err
	}
	r.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
	return k.client.Do(r)
}

func podPath(c claims) string {
	return "/api/v1/namespaces/" + url.PathEscape(c.Namespace) + "/pods/" + url.PathEscape(c.Pod)
}

func (k *kubeClient) owns(ctx context.Context, c claims) bool {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	r, err := k.get(ctx, podPath(c))
	if err != nil {
		return false
	}
	defer r.Body.Close()
	if r.StatusCode != 200 {
		return false
	}
	var pod struct {
		Metadata struct {
			UID    string            `json:"uid"`
			Labels map[string]string `json:"labels"`
			Owners []struct {
				Kind string `json:"kind"`
				UID  string `json:"uid"`
			} `json:"ownerReferences"`
		} `json:"metadata"`
		Spec struct {
			Containers []struct {
				Name string `json:"name"`
			} `json:"containers"`
		} `json:"spec"`
	}
	if json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&pod) != nil || pod.Metadata.UID != c.PodUID || pod.Metadata.Labels["workflows.argoproj.io/workflow"] != c.Workflow {
		return false
	}
	owner, container := false, false
	for _, o := range pod.Metadata.Owners {
		owner = owner || (o.Kind == "Workflow" && o.UID == c.WorkflowUID)
	}
	for _, entry := range pod.Spec.Containers {
		container = container || entry.Name == c.Container
	}
	return owner && container
}

func configuredKube() (*kubeClient, error) {
	const directory = "/var/run/secrets/kubernetes.io/serviceaccount/"
	ca, err := os.ReadFile(directory + "ca.crt")
	if err != nil {
		return nil, err
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		return nil, errors.New("invalid Kubernetes CA")
	}
	endpoint := "https://" + net.JoinHostPort(os.Getenv("KUBERNETES_SERVICE_HOST"), os.Getenv("KUBERNETES_SERVICE_PORT_HTTPS"))
	u, err := url.Parse(endpoint)
	if err != nil || u.Hostname() == "" || u.Port() == "" {
		return nil, errors.New("Kubernetes endpoint required")
	}
	transport := &http.Transport{TLSClientConfig: &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}, DialContext: (&net.Dialer{Timeout: 5 * time.Second}).DialContext, TLSHandshakeTimeout: 5 * time.Second, ResponseHeaderTimeout: 10 * time.Second, IdleConnTimeout: 30 * time.Second, MaxConnsPerHost: 192, MaxIdleConnsPerHost: 64}
	return &kubeClient{client: &http.Client{Transport: transport, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, endpoint: endpoint, tokenFile: directory + "token"}, nil
}
