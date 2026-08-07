import { LoadingIndicator } from "./LoadingIndicator";

type PanelLoadingScreenProps = {
  label: string;
};

export function PanelLoadingScreen({ label }: PanelLoadingScreenProps) {
  return (
    <div className="panel-loading-screen">
      <LoadingIndicator label={label} size={34} />
    </div>
  );
}
