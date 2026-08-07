import { forwardRef } from "react";
import type { SelectHTMLAttributes } from "react";

export const SelectControl = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function SelectControl({ className = "", ...props }, ref) {
    return <select ref={ref} className={`select-control ${className}`.trim()} {...props} />;
  },
);
