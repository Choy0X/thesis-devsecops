import { Folder, FolderPlus } from "lucide-react";
import type { Project } from "./client";

export function ProjectNavigation({ projects, projectId, onAdd, onProject }: {
  projects: Project[]; projectId: string; onAdd: () => void; onProject: (id: string) => void;
}) {
  return <nav className="ide-navigation" aria-label="Projects">
    <div className="ide-region-title"><strong>Projects</strong><button className="ide-icon" aria-label="Add project" onClick={onAdd}><FolderPlus size={16} /></button></div>
    <div className="ide-project-tree">
      {projects.map(project => <div className={`ide-project-heading ${project.id === projectId ? "is-selected" : ""}`} key={project.id}>
        <button title={project.name} aria-current={project.id === projectId ? "true" : undefined} aria-describedby={project.has_active_run ? `project-activity-${project.id}` : undefined} onClick={() => onProject(project.id)}>
          <Folder size={15} aria-hidden /><span>{project.name}</span>
          {project.has_active_run && <span className="ide-project-activity" aria-hidden="true" />}
        </button>
        {project.has_active_run && <span className="sr-only" id={`project-activity-${project.id}`}>Active run</span>}
      </div>)}
      {!projects.length && <p className="ide-rail-hint">Create a project to organize your coding chats.</p>}
    </div>
  </nav>;
}
