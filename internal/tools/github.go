package tools

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/lohitkolluri/KubeWise/pkg/models"
)

// gitHubCommand describes a github subcommand allowed for use.
type gitHubCommand struct {
	capabilities []models.ToolCapability
	readOnly     bool
}

// allowedGitHubSubcommands and their capabilities.
var allowedGitHubSubcommands = map[string]gitHubCommand{
	"get repo":    {[]models.ToolCapability{models.CapRead}, true},
	"get file":    {[]models.ToolCapability{models.CapRead}, true},
	"get pr":      {[]models.ToolCapability{models.CapRead}, true},
	"list prs":    {[]models.ToolCapability{models.CapRead}, true},
	"search prs":  {[]models.ToolCapability{models.CapRead}, true},
	"get issue":   {[]models.ToolCapability{models.CapRead}, true},
	"list issues": {[]models.ToolCapability{models.CapRead}, true},
	"list repos":  {[]models.ToolCapability{models.CapRead}, true},
	"create pr":   {[]models.ToolCapability{models.CapWrite, models.CapRequiresApproval}, false},
	"merge pr":    {[]models.ToolCapability{models.CapWrite, models.CapDestructive, models.CapRequiresApproval}, false},
	"close pr":    {[]models.ToolCapability{models.CapWrite, models.CapDestructive, models.CapRequiresApproval}, false},
	"comment pr":  {[]models.ToolCapability{models.CapWrite, models.CapRequiresApproval}, false},
	"close issue": {[]models.ToolCapability{models.CapWrite}, false},
}

// blockedGitHubSubcommands are explicitly rejected for security reasons.
var blockedGitHubSubcommands = map[string]bool{
	"delete repo":         true,
	"transfer repo":       true,
	"rename repo":         true,
	"add collaborator":    true,
	"remove collaborator": true,
	"create token":        true,
	"delete token":        true,
	"update branch":       true,
	"push":                true,
	"force push":          true,
	"delete branch":       true,
}

var (
	validGitHubOwner = regexp.MustCompile(`^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?$`)
	validGitHubRepo  = regexp.MustCompile(`^[a-zA-Z0-9_\-.]+$`)
	validGitHubRef   = regexp.MustCompile(`^[a-zA-Z0-9_\-./]+$`)
	validPRNumber    = regexp.MustCompile(`^[1-9][0-9]*$`)
	validIssueNumber = regexp.MustCompile(`^[1-9][0-9]*$`)
	validFilePath    = regexp.MustCompile(`^[a-zA-Z0-9_\-./]+$`)
)

// GitHubPlugin wraps GitHub REST API calls with command validation.
// It implements the ToolPlugin interface without requiring the gh CLI.
type GitHubPlugin struct {
	// token is the GitHub personal access token.
	token string
	// baseURL is the GitHub API base URL (default: "https://api.github.com").
	baseURL string
	// httpClient is the HTTP client used for API calls.
	httpClient *http.Client
}

// NewGitHubPlugin creates a new GitHubPlugin with the given token and
// optional base URL. Pass an empty baseURL to use api.github.com.
func NewGitHubPlugin(token, baseURL string) *GitHubPlugin {
	if baseURL == "" {
		baseURL = "https://api.github.com"
	}
	return &GitHubPlugin{
		token:   token,
		baseURL: baseURL,
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

// Name returns the canonical tool name.
func (p *GitHubPlugin) Name() string { return "github" }

// Capabilities returns the set of capabilities this tool supports.
func (p *GitHubPlugin) Capabilities() []models.ToolCapability {
	return []models.ToolCapability{models.CapRead, models.CapWrite, models.CapDestructive, models.CapRequiresApproval}
}

// Validate checks whether the action contains a valid, allowed GitHub command
// with properly formatted parameters.
func (p *GitHubPlugin) Validate(action models.ToolAction) error {
	cmd := action.Command

	if blockedGitHubSubcommands[cmd] {
		return fmt.Errorf("github %q is blocked for security reasons", cmd)
	}

	if _, found := allowedGitHubSubcommands[cmd]; !found {
		return fmt.Errorf("github %q is not in the allowed command list", cmd)
	}

	if owner := action.Args["owner"]; owner != "" {
		if !validGitHubOwner.MatchString(owner) {
			return fmt.Errorf("invalid GitHub owner: %q", owner)
		}
	}

	if repo := action.Args["repo"]; repo != "" {
		if !validGitHubRepo.MatchString(repo) {
			return fmt.Errorf("invalid GitHub repo: %q", repo)
		}
	}

	if ref := action.Args["ref"]; ref != "" {
		if strings.Contains(ref, "..") {
			return fmt.Errorf("invalid git ref: %q", ref)
		}
		if !validGitHubRef.MatchString(ref) {
			return fmt.Errorf("invalid git ref: %q", ref)
		}
	}

	if pr := action.Args["pr"]; pr != "" {
		if !validPRNumber.MatchString(pr) {
			return fmt.Errorf("invalid PR number: %q", pr)
		}
	}

	if issue := action.Args["issue"]; issue != "" {
		if !validIssueNumber.MatchString(issue) {
			return fmt.Errorf("invalid issue number: %q", issue)
		}
	}

	if path := action.Args["path"]; path != "" {
		if strings.Contains(path, "..") {
			return fmt.Errorf("invalid file path: %q", path)
		}
		if !validFilePath.MatchString(path) {
			return fmt.Errorf("invalid file path: %q", path)
		}
	}

	return nil
}

// Execute runs the GitHub command with the given action. It validates the action
// first, then makes the appropriate REST API call.
func (p *GitHubPlugin) Execute(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	if err := p.Validate(action); err != nil {
		return nil, err
	}

	start := time.Now()

	result, err := p.executeAPI(ctx, action)
	if err != nil {
		return &models.ToolResult{
			Success:  false,
			Stderr:   err.Error(),
			Duration: time.Since(start),
		}, nil
	}

	result.Duration = time.Since(start)
	return result, nil
}

// executeAPI dispatches to the appropriate REST API handler based on the command.
func (p *GitHubPlugin) executeAPI(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	switch action.Command {
	case "get repo":
		return p.getRepo(ctx, action)
	case "get file":
		return p.getFile(ctx, action)
	case "get pr":
		return p.getPR(ctx, action)
	case "list prs":
		return p.listPRs(ctx, action)
	case "search prs":
		return p.searchPRs(ctx, action)
	case "get issue":
		return p.getIssue(ctx, action)
	case "list issues":
		return p.listIssues(ctx, action)
	case "list repos":
		return p.listRepos(ctx, action)
	case "create pr":
		return p.createPR(ctx, action)
	case "merge pr":
		return p.mergePR(ctx, action)
	case "close pr":
		return p.closePR(ctx, action)
	case "comment pr":
		return p.commentPR(ctx, action)
	case "close issue":
		return p.closeIssue(ctx, action)
	default:
		return nil, fmt.Errorf("github %q not implemented", action.Command)
	}
}

// --- GitHub REST API helpers ---

func (p *GitHubPlugin) newRequest(ctx context.Context, method, path string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, method, p.baseURL+path, body)
	if err != nil {
		return nil, err
	}
	if p.token != "" {
		req.Header.Set("Authorization", "Bearer "+p.token)
	}
	req.Header.Set("Accept", "application/vnd.github.v3+json")
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	return req, nil
}

func (p *GitHubPlugin) doRequest(req *http.Request) ([]byte, int, error) {
	resp, err := p.httpClient.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("github API request failed: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	body, err := io.ReadAll(io.LimitReader(resp.Body, OutputMaxBytes))
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("reading response body: %w", err)
	}

	if resp.StatusCode >= 400 {
		return body, resp.StatusCode, fmt.Errorf("GitHub API %s %s responded with status %d", req.Method, req.URL.Path, resp.StatusCode)
	}

	return body, resp.StatusCode, nil
}

func (p *GitHubPlugin) getOwnerRepo(action models.ToolAction) (string, string) {
	return action.Args["owner"], action.Args["repo"]
}

// --- Read operations ---

func (p *GitHubPlugin) getRepo(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	if owner == "" || repo == "" {
		return nil, fmt.Errorf("owner and repo are required")
	}
	req, err := p.newRequest(ctx, "GET", "/repos/"+owner+"/"+repo, nil)
	if err != nil {
		return nil, err
	}
	body, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(body),
	}, nil
}

func (p *GitHubPlugin) getFile(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	path := action.Args["path"]
	ref := action.Args["ref"]
	if owner == "" || repo == "" || path == "" {
		return nil, fmt.Errorf("owner, repo, and path are required")
	}
	apiPath := "/repos/" + owner + "/" + repo + "/contents/" + path
	if ref != "" {
		apiPath += "?ref=" + ref
	}
	req, err := p.newRequest(ctx, "GET", apiPath, nil)
	if err != nil {
		return nil, err
	}
	body, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(body),
	}, nil
}

func (p *GitHubPlugin) getPR(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	pr := action.Args["pr"]
	if owner == "" || repo == "" || pr == "" {
		return nil, fmt.Errorf("owner, repo, and pr are required")
	}
	req, err := p.newRequest(ctx, "GET", "/repos/"+owner+"/"+repo+"/pulls/"+pr, nil)
	if err != nil {
		return nil, err
	}
	body, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(body),
	}, nil
}

func (p *GitHubPlugin) listPRs(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	state := action.Args["state"]
	if state == "" {
		state = "open"
	}
	if owner == "" || repo == "" {
		return nil, fmt.Errorf("owner and repo are required")
	}
	req, err := p.newRequest(ctx, "GET", "/repos/"+owner+"/"+repo+"/pulls?state="+state+"&per_page=20", nil)
	if err != nil {
		return nil, err
	}
	body, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(body),
	}, nil
}

func (p *GitHubPlugin) searchPRs(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	query := action.Args["query"]
	if query == "" {
		return nil, fmt.Errorf("search query is required")
	}
	q := url.QueryEscape(query + " type:pr")
	req, err := p.newRequest(ctx, "GET", "/search/issues?q="+q+"&per_page=20", nil)
	if err != nil {
		return nil, err
	}
	body, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(body),
	}, nil
}

func (p *GitHubPlugin) getIssue(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	issue := action.Args["issue"]
	if owner == "" || repo == "" || issue == "" {
		return nil, fmt.Errorf("owner, repo, and issue are required")
	}
	req, err := p.newRequest(ctx, "GET", "/repos/"+owner+"/"+repo+"/issues/"+issue, nil)
	if err != nil {
		return nil, err
	}
	body, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(body),
	}, nil
}

func (p *GitHubPlugin) listIssues(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	state := action.Args["state"]
	if state == "" {
		state = "open"
	}
	if owner == "" || repo == "" {
		return nil, fmt.Errorf("owner and repo are required")
	}
	req, err := p.newRequest(ctx, "GET", "/repos/"+owner+"/"+repo+"/issues?state="+state+"&per_page=20", nil)
	if err != nil {
		return nil, err
	}
	body, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(body),
	}, nil
}

func (p *GitHubPlugin) listRepos(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner := action.Args["owner"]
	typ := action.Args["type"]
	if typ == "" {
		typ = "all"
	}
	req, err := p.newRequest(ctx, "GET", "/orgs/"+owner+"/repos?type="+typ+"&per_page=20", nil)
	if err != nil {
		return nil, err
	}
	// If no owner, list authenticated user's repos
	if owner == "" {
		req, err = p.newRequest(ctx, "GET", "/user/repos?per_page=20", nil)
		if err != nil {
			return nil, err
		}
	}
	body, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(body),
	}, nil
}

// --- Write operations ---

type createPRBody struct {
	Title string `json:"title"`
	Head  string `json:"head"`
	Base  string `json:"base"`
	Body  string `json:"body,omitempty"`
	Draft bool   `json:"draft,omitempty"`
}

func (p *GitHubPlugin) createPR(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	title := action.Args["title"]
	head := action.Args["head"]
	base := action.Args["base"]
	if owner == "" || repo == "" || title == "" || head == "" || base == "" {
		return nil, fmt.Errorf("owner, repo, title, head, and base are required")
	}

	body := createPRBody{
		Title: title,
		Head:  head,
		Base:  base,
		Body:  action.Args["body"],
	}
	if action.Args["draft"] == "true" {
		body.Draft = true
	}

	payload, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshalling create PR body: %w", err)
	}

	req, err := p.newRequest(ctx, "POST", "/repos/"+owner+"/"+repo+"/pulls", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	respBody, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(respBody),
	}, nil
}

func (p *GitHubPlugin) mergePR(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	pr := action.Args["pr"]
	if owner == "" || repo == "" || pr == "" {
		return nil, fmt.Errorf("owner, repo, and pr are required")
	}

	var payload io.Reader
	if msg := action.Args["message"]; msg != "" {
		body := map[string]string{"commit_message": msg}
		data, _ := json.Marshal(body)
		payload = bytes.NewReader(data)
	}

	req, err := p.newRequest(ctx, "PUT", "/repos/"+owner+"/"+repo+"/pulls/"+pr+"/merge", payload)
	if err != nil {
		return nil, err
	}
	respBody, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(respBody),
	}, nil
}

func (p *GitHubPlugin) closePR(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	pr := action.Args["pr"]
	if owner == "" || repo == "" || pr == "" {
		return nil, fmt.Errorf("owner, repo, and pr are required")
	}

	body := map[string]string{"state": "closed"}
	payload, _ := json.Marshal(body)

	req, err := p.newRequest(ctx, "PATCH", "/repos/"+owner+"/"+repo+"/pulls/"+pr, bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	respBody, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(respBody),
	}, nil
}

type commentBody struct {
	Body string `json:"body"`
}

func (p *GitHubPlugin) commentPR(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	pr := action.Args["pr"]
	comment := action.Args["body"]
	if owner == "" || repo == "" || pr == "" || comment == "" {
		return nil, fmt.Errorf("owner, repo, pr, and body are required")
	}

	payload, err := json.Marshal(commentBody{Body: comment})
	if err != nil {
		return nil, fmt.Errorf("marshalling comment body: %w", err)
	}

	req, err := p.newRequest(ctx, "POST", "/repos/"+owner+"/"+repo+"/issues/"+pr+"/comments", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	respBody, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(respBody),
	}, nil
}

func (p *GitHubPlugin) closeIssue(ctx context.Context, action models.ToolAction) (*models.ToolResult, error) {
	owner, repo := p.getOwnerRepo(action)
	issue := action.Args["issue"]
	comment := action.Args["comment"]
	if owner == "" || repo == "" || issue == "" {
		return nil, fmt.Errorf("owner, repo, and issue are required")
	}

	var closePayload map[string]interface{}
	if comment != "" {
		closePayload = map[string]interface{}{
			"state":        "closed",
			"state_reason": "completed",
		}
	} else {
		closePayload = map[string]interface{}{
			"state": "closed",
		}
	}

	payload, _ := json.Marshal(closePayload)

	// First, post a comment if provided
	if comment != "" {
		commentPayload, _ := json.Marshal(commentBody{Body: comment})
		commentReq, err := p.newRequest(ctx, "POST", "/repos/"+owner+"/"+repo+"/issues/"+issue+"/comments", bytes.NewReader(commentPayload))
		if err != nil {
			return nil, err
		}
		_, _, err = p.doRequest(commentReq)
		if err != nil {
			// Non-fatal: try to close anyway
			_ = err
		}
	}

	req, err := p.newRequest(ctx, "PATCH", "/repos/"+owner+"/"+repo+"/issues/"+issue, bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	respBody, _, err := p.doRequest(req)
	if err != nil {
		return nil, err
	}
	return &models.ToolResult{
		Success: true,
		Stdout:  string(respBody),
	}, nil
}
